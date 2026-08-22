#!/usr/bin/env python3
"""Rebuild the human-facing PRA reporting-estate catalogue.

The official Bank reporting page is the authority for which file is a data
item/template and which is an instruction.  File inspection enriches that
classification; filename regexes never override it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.migrations import apply_migrations
try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect


DB_PATH = PROJECT_ROOT / "backend/data/rulebook.sqlite3"
RAW_DIR = PROJECT_ROOT / "backend/data/raw/reporting-sources/official-catalog"
SOURCE_PAGE = (
    "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting/"
    "regulatory-reporting-banking-sector/banks-building-societies-and-investment-firms"
)
HTTP_HEADERS = {"User-Agent": "PRA-Rulebook-Explorer/1.0 (+reporting-catalogue)"}
NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

TABLE_SPECS = {
    0: ("supervisory_reporting", "CRR supervisory reporting", True),
    2: ("pillar3_disclosure", "Pillar 3 disclosures", True),
    3: ("supervisory_reporting", "PRA data items", True),
    4: ("supervisory_reporting", "Ring-fencing returns", False),
    6: ("supervisory_reporting", "FSA returns", False),
    9: ("supervisory_reporting", "Mortgage reporting", True),
}


def clean_url(url: str) -> str:
    absolute = urljoin(SOURCE_PAGE, url)
    parts = urlsplit(absolute)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def stable(prefix: str, *parts: object) -> str:
    value = "|".join(str(part or "") for part in parts)
    return f"{prefix}:{hashlib.sha1(value.encode()).hexdigest()[:16]}"


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extension(url: str) -> str:
    name = Path(urlsplit(url).path).name.lower()
    suffix = Path(name).suffix.lstrip(".")
    return suffix or "html"


def sheet_names(path: Path | None) -> list[str]:
    if not path or not path.exists() or path.suffix.lower() not in {".xlsx", ".xlsm", ".xltx"}:
        return []
    try:
        with zipfile.ZipFile(path) as archive:
            from xml.etree import ElementTree as ET

            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            return [
                compact(sheet.attrib.get("name", "Sheet"))
                for sheet in workbook.findall(f"{NS_MAIN}sheets/{NS_MAIN}sheet")
            ]
    except (OSError, KeyError, zipfile.BadZipFile):
        return []


def inferred_dates(text: str) -> tuple[str | None, str | None, str]:
    values = re.findall(r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text or "")
    parsed: list[date] = []
    for value in values[:2]:
        try:
            parsed.append(datetime.strptime(value, "%d %B %Y").date())
        except ValueError:
            pass
    start = parsed[0].isoformat() if parsed else None
    end = parsed[1].isoformat() if len(parsed) > 1 else None
    today = date.today()
    status = "future" if parsed and parsed[0] > today else "historic" if end and parsed[1] < today else "current"
    if "no longer effective" in (text or "").lower():
        status = "historic"
    return start, end, status


def local_source(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    target = clean_url(url).lower()
    for row in conn.execute(
        "SELECT source_id,title,url,local_path,file_type FROM source_document WHERE url NOT LIKE '%#%'"
    ):
        if clean_url(row["url"]).lower() == target:
            return row
    return None


def download_source(conn: sqlite3.Connection, url: str, title: str) -> sqlite3.Row:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=90, headers=HTTP_HEADERS)
    response.raise_for_status()
    digest = hashlib.sha256(response.content).hexdigest()
    filename = Path(urlsplit(url).path).name or f"{digest[:16]}.bin"
    path = RAW_DIR / f"{digest[:12]}-{filename}"
    path.write_bytes(response.content)
    source_id = stable("source", url, digest)
    rel_path = path.relative_to(PROJECT_ROOT).as_posix()
    conn.execute(
        """
        INSERT OR IGNORE INTO source_document(
          source_id,title,url,local_path,file_type,checksum_sha256,downloaded_at,
          parent_url,source_status,notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_id,
            title,
            url,
            rel_path,
            extension(url),
            digest,
            datetime.now(timezone.utc).isoformat(),
            SOURCE_PAGE,
            "downloaded",
            "Downloaded from the authoritative PRA reporting catalogue.",
        ),
    )
    return conn.execute(
        "SELECT source_id,title,url,local_path,file_type FROM source_document WHERE source_id=?", (source_id,)
    ).fetchone()


def return_code(table_index: int, cells: list[str], row_index: int) -> str:
    first = compact(cells[0]).upper()
    if table_index in {3, 4, 6}:
        return re.sub(r"[^A-Z0-9-]", "", first)
    if table_index == 9:
        return "MLAR"
    annex = re.sub(r"[^A-Z0-9]+", "-", first).strip("-")
    prefix = "DISCLOSURE" if table_index == 2 else "CRR"
    return f"{prefix}-{annex or row_index}"


def table_rows(soup: BeautifulSoup) -> list[tuple[int, object]]:
    result = []
    for index, table in enumerate(soup.find_all("table")):
        if index not in TABLE_SPECS:
            continue
        rows = table.find_all("tr", recursive=False)
        if not rows:
            rows = table.find_all("tr")
        for row_index, row in enumerate(rows[1:], 1):
            result.append((index, (row_index, row)))
    return result


def artifact(
    conn: sqlite3.Connection,
    *,
    url: str,
    title: str,
    role: str,
    estate: str,
    description: str,
    download_missing: bool,
) -> str:
    url = clean_url(url)
    source = local_source(conn, url)
    if source is None and download_missing:
        source = download_source(conn, url, title)
    local_path = PROJECT_ROOT / source["local_path"] if source and source["local_path"] else None
    names = sheet_names(local_path)
    file_type = (source["file_type"] if source else None) or extension(url)
    artifact_id = stable("reporting_artifact", url, role)
    conn.execute(
        """
        INSERT INTO reporting_artifact(
          artifact_id,source_id,url,display_title,artifact_role,estate,file_type,
          sheet_names_json,extracted_title,description,classification_method,
          classification_confidence,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(artifact_id) DO UPDATE SET
          source_id=excluded.source_id,url=excluded.url,display_title=excluded.display_title,
          artifact_role=excluded.artifact_role,estate=excluded.estate,file_type=excluded.file_type,
          sheet_names_json=excluded.sheet_names_json,extracted_title=excluded.extracted_title,
          description=excluded.description,classification_method=excluded.classification_method,
          classification_confidence=excluded.classification_confidence,updated_at=CURRENT_TIMESTAMP
        """,
        (
            artifact_id,
            source["source_id"] if source else None,
            url,
            title,
            role,
            estate,
            file_type,
            json.dumps(names, ensure_ascii=False),
            source["title"] if source else None,
            description,
            "official_pra_table_column+file_inspection",
            1.0,
        ),
    )
    return artifact_id


def sync_graph(conn: sqlite3.Connection) -> dict[str, int]:
    """Project authoritative catalogue relationships into the generic graph."""
    removed = 0
    for code in ("COREP-CCR", "COREP-LEVERAGE", "COREP-MARKET-RISK", "PILLAR3-DISCLOSURE"):
        for node_id in (f"data_item:{code}", f"reporting_obligation:{code}", f"template_set:{code}", f"instruction_set:{code}"):
            removed += conn.execute("DELETE FROM graph_node WHERE node_id=?", (node_id,)).rowcount
    conn.execute("DELETE FROM graph_edge WHERE extraction_method='official_reporting_catalog'")
    conn.execute("DELETE FROM graph_node WHERE source_table='reporting_return_catalog'")
    nodes = edges = 0
    rows = list(conn.execute(
        """SELECT * FROM reporting_return_catalog
           ORDER BY CASE status WHEN 'current' THEN 1 WHEN 'future' THEN 2 ELSE 3 END,effective_from"""
    ).fetchall())

    # Every published version receives its own graph root so selecting a
    # future or superseded entry never shows the current version's files.
    for row in rows:
        if row["estate"] == "pillar3_disclosure":
            node_id = f"disclosure_set:{row['return_id']}"
            node_type = "DisclosureSet"
        else:
            node_id = f"return_version:{row['return_id']}"
            node_type = "ReportingReturn"
        projected_nodes, projected_edges = _project_catalog_row(
            conn, row, node_id=node_id, node_type=node_type,
            source_table="reporting_return_catalog", source_pk=row["return_id"],
        )
        nodes += projected_nodes
        edges += projected_edges

    # Preserve one current DataItem alias per supervisory code for existing API
    # consumers and historical graph links. The reporting UI uses the exact
    # version roots above.
    current_supervisory_codes = {
        row["return_code"] for row in rows
        if row["estate"] == "supervisory_reporting" and row["status"] == "current"
    }
    projected_supervisory_codes: set[str] = set()
    for row in rows:
        code = row["return_code"]
        if row["estate"] != "supervisory_reporting" or code in projected_supervisory_codes:
            continue
        if row["status"] == "future" and code in current_supervisory_codes:
            continue
        projected_supervisory_codes.add(code)
        node_id = f"data_item:{code}"
        existing = conn.execute("SELECT source_table,source_pk FROM graph_node WHERE node_id=?", (node_id,)).fetchone()
        alias_source_table = existing["source_table"] if existing and existing["source_table"] != "reporting_return_catalog" else "reporting_return_catalog_alias"
        alias_source_pk = existing["source_pk"] if existing and existing["source_table"] != "reporting_return_catalog" else code
        projected_nodes, projected_edges = _project_catalog_row(
            conn, row, node_id=node_id, node_type="DataItem",
            source_table=alias_source_table,
            source_pk=alias_source_pk,
        )
        nodes += projected_nodes
        edges += projected_edges
    return {"graph_nodes_projected": nodes, "graph_edges_projected": edges, "synthetic_nodes_removed": removed}


def _project_catalog_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    node_id: str,
    node_type: str,
    source_table: str,
    source_pk: str,
) -> tuple[int, int]:
    props = {
        "data_item_code": row["return_code"],
        "return_id": row["return_id"],
        "name": row["name"],
        "description": row["description"],
        "estate": row["estate"],
        "family": row["family"],
        "status": row["status"],
        "effective_text": row["effective_text"],
        "source_page_url": row["source_page_url"],
    }
    conn.execute(
        """
        INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json,effective_from,effective_to,review_status)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(node_id) DO UPDATE SET node_type=excluded.node_type,label=excluded.label,
          properties_json=excluded.properties_json,effective_from=excluded.effective_from,
          effective_to=excluded.effective_to,review_status=excluded.review_status
        """,
        (node_id, node_type, f"{row['return_code']} — {row['name']}", source_table, source_pk, json.dumps(props), row["effective_from"], row["effective_to"], "accepted_candidate"),
    )
    edges = 0
    for linked in conn.execute(
            """
            SELECT a.*,ra.relationship FROM reporting_return_artifact ra
            JOIN reporting_artifact a ON a.artifact_id=ra.artifact_id
            WHERE ra.return_id=? AND a.source_id IS NOT NULL
            """,
            (row["return_id"],),
        ).fetchall():
        source_node = f"source_document:{linked['source_id']}"
        source_props = {
            "url": linked["url"], "file_type": linked["file_type"],
            "reporting_role": linked["artifact_role"], "estate": linked["estate"],
            "display_title": linked["display_title"], "description": linked["description"],
            "sheet_names": json.loads(linked["sheet_names_json"] or "[]"),
        }
        conn.execute(
            """
            INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json,review_status)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET label=excluded.label,properties_json=excluded.properties_json,review_status=excluded.review_status
            """,
            (source_node, "SourceDocument", linked["display_title"], "source_document", linked["source_id"], json.dumps(source_props), "accepted_candidate"),
        )
        edge = stable("edge", node_id, "EVIDENCED_BY", source_node, linked["relationship"], row["return_id"])
        conn.execute(
            """
            INSERT OR REPLACE INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,properties_json,confidence,extraction_method,review_status)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (edge, node_id, source_node, "EVIDENCED_BY", json.dumps({"relationship": linked["relationship"], "official_catalog": True}), 1.0, "official_reporting_catalog", "accepted_candidate"),
        )
        edges += 1
    return 1, edges


def rebuild(*, download_missing: bool) -> dict[str, int]:
    response = requests.get(SOURCE_PAGE, timeout=90, headers=HTTP_HEADERS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    conn = connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    conn.execute("DELETE FROM reporting_return_artifact")
    conn.execute("DELETE FROM reporting_return_catalog")
    conn.execute("DELETE FROM reporting_artifact")
    counts = {"returns": 0, "template": 0, "instructions": 0, "downloaded": 0}

    for table_index, packed in table_rows(soup):
        row_index, row = packed
        cells_raw = row.find_all(["th", "td"], recursive=False)
        if not cells_raw:
            cells_raw = row.find_all(["th", "td"])
        cells = [compact(cell.get_text(" ", strip=True)) for cell in cells_raw]
        if len(cells) < 3 or not any(cell.find("a", href=True) for cell in cells_raw):
            continue
        estate, family, has_effective = TABLE_SPECS[table_index]
        code = return_code(table_index, cells, row_index)
        name_index = 1 if table_index != 9 else 0
        name = cells[name_index] or code
        effective_text = cells[-1] if has_effective and len(cells) >= 4 else ""
        start, end, status = inferred_dates(effective_text)
        rid = stable("reporting_return", code, name, effective_text)
        conn.execute(
            """
            INSERT INTO reporting_return_catalog(
              return_id,return_code,name,description,estate,family,effective_from,
              effective_to,effective_text,source_page_url,source_table,source_row,status,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (rid, code, name, name, estate, family, start, end, effective_text, SOURCE_PAGE, table_index, row_index, status),
        )
        counts["returns"] += 1

        link_cells = cells_raw[2:4] if table_index != 9 else cells_raw[1:3]
        for order, (role, cell) in enumerate(zip(("template", "instructions"), link_cells), 1):
            for link_order, link in enumerate(cell.find_all("a", href=True)):
                href = clean_url(link["href"])
                label = compact(link.get_text(" ", strip=True)) or cells_raw[order + 1].get_text(" ", strip=True)
                artifact_role = f"disclosure_{role}" if estate == "pillar3_disclosure" else role
                before = local_source(conn, href)
                aid = artifact(
                    conn,
                    url=href,
                    title=label,
                    role=artifact_role,
                    estate=estate,
                    description=f"{role.title()} for {code}: {name}",
                    download_missing=download_missing,
                )
                if before is None and local_source(conn, href) is not None:
                    counts["downloaded"] += 1
                conn.execute(
                    """
                    INSERT INTO reporting_return_artifact(return_id,artifact_id,relationship,is_primary,display_order)
                    VALUES (?,?,?,?,?)
                    """,
                    (rid, aid, role, 1, order * 100 + link_order),
                )
                counts[role] += 1

    # Taxonomy and XBRL packages are estate-wide technical artefacts, not
    # synthetic returns.  Keep them searchable without pretending that each
    # XML schema is a reporting form.
    for link in soup.find_all("a", href=True):
        label = compact(link.get_text(" ", strip=True))
        href = clean_url(link["href"])
        hay = f"{label} {href}".lower()
        if not re.search(r"\b(taxonomy|xbrl|dpm|validation rules|sample instance)\b", hay):
            continue
        if extension(href) not in {"zip", "xlsx", "xls", "pdf", "xml", "xsd", "xbrl"}:
            continue
        role = "taxonomy" if re.search(r"taxonomy|dpm", hay) else "validation" if "validation" in hay else "supporting_tool"
        artifact(
            conn,
            url=href,
            title=label or Path(urlsplit(href).path).name,
            role=role,
            estate="technical",
            description="Estate-wide reporting taxonomy or technical support file.",
            download_missing=download_missing,
        )

    conn.commit()
    counts.update(sync_graph(conn))
    conn.commit()
    counts["artifacts"] = conn.execute("SELECT COUNT(*) FROM reporting_artifact").fetchone()[0]
    counts["with_local_source"] = conn.execute(
        "SELECT COUNT(*) FROM reporting_artifact WHERE source_id IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-missing", action="store_true")
    args = parser.parse_args()
    print(json.dumps(rebuild(download_missing=args.download_missing), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()