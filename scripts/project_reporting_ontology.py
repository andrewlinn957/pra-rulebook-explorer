#!/usr/bin/env python3
"""Project the reporting catalogue into the PRA Reporting Estate Ontology."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.db import connect
from backend.app.migrations import apply_migrations


DB_PATH = ROOT / "backend/data/rulebook.sqlite3"
SUPPORTING_SHEET = re.compile(
    r"^(cover|index|contents?|definitions?|drop[ -]?down|validation|checks?|read ?me|notes?|ref(?:erence)?s?|mapping|version|metadata)$",
    re.I,
)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "other"


def stable(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return f"{prefix}:{hashlib.sha1(raw.encode()).hexdigest()[:16]}"


def props(**values: Any) -> str:
    return json.dumps({key: value for key, value in values.items() if value not in (None, "")}, ensure_ascii=False)


def taxonomy_version(value: str) -> str | None:
    text = str(value or "")
    match = re.search(r"\bv\.?\s*(\d)[._-]?(\d)[._-]?(\d)\b", text, re.I)
    if match:
        return ".".join(match.groups())
    match = re.search(r"(?:taxonomy|banking|dpm|validations?)[-_ ]v?(\d)(\d)(\d)(?:\D|$)", text, re.I)
    return ".".join(match.groups()) if match else None


def normalized_role(role: str) -> str:
    return {
        "template": "reporting_template",
        "instructions": "reporting_instructions",
        "disclosure_template": "disclosure_template",
        "disclosure_instructions": "disclosure_instructions",
        "taxonomy": "xbrl_taxonomy_resource",
        "validation": "validation_rules",
        "supporting_tool": "supporting_tool",
    }.get(role, role)


def subject_tags(value: str) -> list[str]:
    text = str(value or "").lower()
    rules = {
        "capital": r"capital|own funds|g-sii|loss absorbing",
        "liquidity": r"liquidity|lcr|nsfr|stable funding|intraday",
        "leverage": r"leverage",
        "credit risk": r"credit risk|counterparty|immovable property|large exposure",
        "market risk": r"market risk",
        "financial reporting": r"finrep|financial information|financial statement|asset encumbrance",
        "operational resilience": r"operational risk|operational resilience",
        "ring-fencing": r"ring.?fenc|intragroup|excluded activit",
        "mortgage reporting": r"mortgage|mlar",
    }
    return [name for name, pattern in rules.items() if re.search(pattern, text)]


def rebuild(conn: sqlite3.Connection) -> dict[str, int]:
    apply_migrations(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    for table in (
        "reporting_edition_taxonomy", "reporting_taxonomy_resource", "reporting_taxonomy_release",
        "reporting_resource_component", "reporting_edition_resource", "reporting_resource",
        "reporting_requirement_edition", "reporting_requirement", "reporting_collection", "reporting_regime",
    ):
        conn.execute(f"DELETE FROM {table}")

    regimes = {
        "supervisory_reporting": (
            "regime:regulatory_reporting", "Regulatory reporting",
            "Information submitted to the PRA for supervisory purposes.", 10,
        ),
        "pillar3_disclosure": (
            "regime:pillar3_disclosure", "Pillar 3 disclosure",
            "Information published under Pillar 3 disclosure requirements.", 20,
        ),
        "technical": (
            "regime:shared_reporting_infrastructure", "Shared reporting infrastructure",
            "XBRL taxonomies, DPM packages, validations, sample instances, filing rules and utilities.", 30,
        ),
    }
    for regime_id, name, description, order in regimes.values():
        conn.execute(
            "INSERT INTO reporting_regime(regime_id,name,description,sort_order) VALUES (?,?,?,?)",
            (regime_id, name, description, order),
        )

    collection_ids: dict[tuple[str, str], str] = {}
    families = conn.execute(
        "SELECT DISTINCT estate,family FROM reporting_return_catalog ORDER BY estate,family"
    ).fetchall()
    for order, row in enumerate(families, 1):
        regime_id = regimes[row["estate"]][0]
        collection_id = f"collection:{slug(row['family'])}"
        collection_ids[(row["estate"], row["family"])] = collection_id
        conn.execute(
            "INSERT INTO reporting_collection(collection_id,regime_id,name,description,sort_order) VALUES (?,?,?,?,?)",
            (collection_id, regime_id, row["family"], f"Official {row['family']} collection.", order * 10),
        )
    technical_collection = "collection:shared_xbrl_infrastructure"
    conn.execute(
        "INSERT INTO reporting_collection(collection_id,regime_id,name,description,sort_order) VALUES (?,?,?,?,?)",
        (technical_collection, regimes["technical"][0], "Shared XBRL infrastructure", "Banking taxonomy and technical reporting resources.", 10),
    )

    catalogue = conn.execute(
        """SELECT * FROM reporting_return_catalog
           ORDER BY CASE status WHEN 'current' THEN 1 WHEN 'future' THEN 2 ELSE 3 END,effective_from"""
    ).fetchall()
    requirement_ids: dict[tuple[str, str], str] = {}
    for row in catalogue:
        collection_id = collection_ids[(row["estate"], row["family"])]
        key = (collection_id, row["return_code"])
        requirement_id = requirement_ids.setdefault(key, f"requirement:{slug(row['return_code'])}")
        requirement_type = "disclosure_requirement" if row["estate"] == "pillar3_disclosure" else "regulatory_return"
        conn.execute(
            """INSERT INTO reporting_requirement(
                 requirement_id,collection_id,requirement_type,code,name,description,subject_tags_json
               ) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(requirement_id) DO NOTHING""",
            (
                requirement_id, collection_id, requirement_type, row["return_code"], row["name"],
                row["description"], json.dumps(subject_tags(f"{row['name']} {row['description']}")),
            ),
        )
        edition_id = row["return_id"].replace("reporting_return:", "edition:", 1)
        conn.execute(
            """INSERT INTO reporting_requirement_edition(
                 edition_id,requirement_id,official_name,description,effective_from,effective_to,
                 effective_text,status,source_page_url,legacy_return_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                edition_id, requirement_id, row["name"], row["description"], row["effective_from"],
                row["effective_to"], row["effective_text"],
                "superseded" if row["status"] == "historic" else row["status"],
                row["source_page_url"], row["return_id"],
            ),
        )

    artifact_to_resource: dict[str, str] = {}
    for artifact in conn.execute("SELECT * FROM reporting_artifact ORDER BY artifact_id"):
        resource_id = artifact["artifact_id"].replace("reporting_artifact:", "resource:", 1)
        artifact_to_resource[artifact["artifact_id"]] = resource_id
        conn.execute(
            """INSERT INTO reporting_resource(
                 resource_id,source_id,resource_role,file_format,title,description,url,legacy_artifact_id
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                resource_id, artifact["source_id"], normalized_role(artifact["artifact_role"]),
                artifact["file_type"], artifact["display_title"], artifact["description"],
                artifact["url"], artifact["artifact_id"],
            ),
        )
        try:
            sheets = json.loads(artifact["sheet_names_json"] or "[]")
        except json.JSONDecodeError:
            sheets = []
        for index, sheet_name in enumerate(sheets):
            component_role = "supporting_worksheet" if SUPPORTING_SHEET.match(str(sheet_name).strip()) else "reporting_worksheet"
            worksheet_id = stable("worksheet", resource_id, sheet_name)
            conn.execute(
                """INSERT INTO reporting_resource_component(
                     component_id,resource_id,component_type,component_role,code,name,sort_order,metadata_json
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (worksheet_id, resource_id, "worksheet", component_role, None, sheet_name, index, "{}"),
            )
            if component_role == "reporting_worksheet":
                template_id = stable("logical_template", resource_id, sheet_name)
                conn.execute(
                    """INSERT INTO reporting_resource_component(
                         component_id,resource_id,parent_component_id,component_type,component_role,code,name,sort_order,metadata_json
                       ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (template_id, resource_id, worksheet_id, "logical_template", "reporting_template", sheet_name, sheet_name, index, "{}"),
                )
        if normalized_role(artifact["artifact_role"]) in {"reporting_instructions", "disclosure_instructions"}:
            section_id = stable("instruction_section", resource_id, "instructions")
            conn.execute(
                """INSERT INTO reporting_resource_component(
                     component_id,resource_id,component_type,component_role,code,name,sort_order,metadata_json
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (section_id, resource_id, "instruction_section", "document_level", None, artifact["display_title"], 0, "{}"),
            )

    for link in conn.execute("SELECT * FROM reporting_return_artifact ORDER BY return_id,display_order"):
        edition_id = link["return_id"].replace("reporting_return:", "edition:", 1)
        resource_id = artifact_to_resource[link["artifact_id"]]
        conn.execute(
            "INSERT INTO reporting_edition_resource VALUES (?,?,?,?,?)",
            (edition_id, resource_id, link["relationship"], link["is_primary"], link["display_order"]),
        )

    releases: dict[str, str] = {}
    technical = conn.execute(
        """SELECT a.* FROM reporting_artifact a
           WHERE a.estate='technical' ORDER BY a.display_title"""
    ).fetchall()
    for artifact in technical:
        version = taxonomy_version(f"{artifact['display_title']} {artifact['url']}")
        if not version:
            continue
        release_id = releases.setdefault(version, f"taxonomy_release:banking:{version}")
        conn.execute(
            "INSERT OR IGNORE INTO reporting_taxonomy_release(release_id,name,version,description) VALUES (?,?,?,?)",
            (release_id, f"Bank of England Banking Taxonomy v{version}", version, f"Banking XBRL taxonomy release v{version}."),
        )
        conn.execute(
            "INSERT OR IGNORE INTO reporting_taxonomy_resource VALUES (?,?,?)",
            (release_id, artifact_to_resource[artifact["artifact_id"]], normalized_role(artifact["artifact_role"])),
        )

    # Link editions to taxonomy releases only when a corpus source path names
    # both the return code and an identifiable taxonomy version.
    source_rows = conn.execute(
        """SELECT lower(coalesce(title,'') || ' ' || coalesce(url,'') || ' ' || coalesce(local_path,'')) AS hay
           FROM source_document
           WHERE file_type IN ('xml','xsd','xbrl','zip') AND lower(coalesce(url,'') || ' ' || coalesce(local_path,'')) LIKE '%banking%'"""
    ).fetchall()
    version_hays: dict[str, list[str]] = {}
    for source in source_rows:
        version = taxonomy_version(source["hay"])
        if version in releases:
            version_hays.setdefault(version, []).append(source["hay"])
    for edition in conn.execute(
        """SELECT e.edition_id,r.code FROM reporting_requirement_edition e
           JOIN reporting_requirement r ON r.requirement_id=e.requirement_id
           WHERE r.requirement_type='regulatory_return'"""
    ):
        code = edition["code"].lower()
        if not re.fullmatch(r"[a-z]+\d+[a-z]?", code):
            continue
        for version, haystacks in version_hays.items():
            if any(re.search(rf"(?<![a-z0-9]){re.escape(code)}(?![a-z0-9])", hay) for hay in haystacks):
                conn.execute(
                    "INSERT OR IGNORE INTO reporting_edition_taxonomy VALUES (?,?,?,?)",
                    (edition["edition_id"], releases[version], "supported_by", "Return code and taxonomy version co-occur in an extracted taxonomy source path."),
                )

    # Materialise one evidenced entry-point component per return code and
    # taxonomy release. Entry points attach to a package resource and encode
    # the stable requirement rather than a physical workbook.
    for link in conn.execute(
        """SELECT et.edition_id,et.release_id,e.requirement_id,r.code,
                  (SELECT tr.resource_id FROM reporting_taxonomy_resource tr
                   JOIN reporting_resource res ON res.resource_id=tr.resource_id
                   WHERE tr.release_id=et.release_id
                   ORDER BY CASE WHEN lower(res.title) LIKE '%package%' THEN 0 ELSE 1 END,res.title
                   LIMIT 1) AS resource_id
           FROM reporting_edition_taxonomy et
           JOIN reporting_requirement_edition e ON e.edition_id=et.edition_id
           JOIN reporting_requirement r ON r.requirement_id=e.requirement_id"""
    ):
        if not link["resource_id"]:
            continue
        entry_id = stable("taxonomy_entry_point", link["release_id"], link["code"])
        conn.execute(
            """INSERT OR IGNORE INTO reporting_resource_component(
                 component_id,resource_id,component_type,component_role,code,name,sort_order,metadata_json
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                entry_id, link["resource_id"], "taxonomy_entry_point", "reporting_module",
                link["code"], f"{link['code']} taxonomy entry point", 0,
                json.dumps({"release_id": link["release_id"], "requirement_id": link["requirement_id"]}),
            ),
        )

    apply_display_name_overrides(conn)

    counts = project_graph(conn)
    conn.commit()
    counts.update({
        "regimes": conn.execute("SELECT COUNT(*) FROM reporting_regime").fetchone()[0],
        "collections": conn.execute("SELECT COUNT(*) FROM reporting_collection").fetchone()[0],
        "requirements": conn.execute("SELECT COUNT(*) FROM reporting_requirement").fetchone()[0],
        "editions": conn.execute("SELECT COUNT(*) FROM reporting_requirement_edition").fetchone()[0],
        "resources": conn.execute("SELECT COUNT(*) FROM reporting_resource").fetchone()[0],
        "components": conn.execute("SELECT COUNT(*) FROM reporting_resource_component").fetchone()[0],
        "taxonomy_releases": conn.execute("SELECT COUNT(*) FROM reporting_taxonomy_release").fetchone()[0],
        "edition_taxonomy_links": conn.execute("SELECT COUNT(*) FROM reporting_edition_taxonomy").fetchone()[0],
    })
    return counts


def apply_display_name_overrides(conn: sqlite3.Connection) -> int:
    tables = {
        "regime": ("reporting_regime", "regime_id"),
        "collection": ("reporting_collection", "collection_id"),
        "requirement": ("reporting_requirement", "requirement_id"),
        "edition": ("reporting_requirement_edition", "edition_id"),
        "resource": ("reporting_resource", "resource_id"),
        "component": ("reporting_resource_component", "component_id"),
        "taxonomy_release": ("reporting_taxonomy_release", "release_id"),
    }
    applied = 0
    for override in conn.execute("SELECT entity_type,entity_id,display_name FROM reporting_display_name_override"):
        table, key = tables[override["entity_type"]]
        applied += conn.execute(
            f"UPDATE {table} SET display_name=? WHERE {key}=?",
            (override["display_name"], override["entity_id"]),
        ).rowcount
    return applied


def project_graph(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute("DELETE FROM graph_edge WHERE extraction_method='reporting_ontology'")
    conn.execute("DELETE FROM graph_node WHERE source_table LIKE 'reporting_ontology:%'")
    nodes = edges = 0

    def node(node_id: str, node_type: str, label: str, table: str, source_pk: str, properties: dict[str, Any]) -> None:
        nonlocal nodes
        conn.execute(
            """INSERT OR REPLACE INTO graph_node(
                 node_id,node_type,label,source_table,source_pk,properties_json,review_status
               ) VALUES (?,?,?,?,?,?,?)""",
            (node_id, node_type, label, f"reporting_ontology:{table}", source_pk, json.dumps(properties, ensure_ascii=False), "accepted_candidate"),
        )
        nodes += 1

    def edge(source: str, relationship: str, target: str, properties: dict[str, Any] | None = None) -> None:
        nonlocal edges
        edge_id = stable("edge", "reporting_ontology", source, relationship, target)
        conn.execute(
            """INSERT OR REPLACE INTO graph_edge(
                 edge_id,source_node_id,target_node_id,edge_type,properties_json,confidence,extraction_method,review_status
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (edge_id, source, target, relationship, json.dumps(properties or {}), 1.0, "reporting_ontology", "accepted_candidate"),
        )
        edges += 1

    estate_id = "reporting_estate:pra"
    node(estate_id, "ReportingEstate", "PRA reporting estate", "estate", "pra", {"description": "The PRA regulatory reporting, disclosure and shared technical estate."})
    for row in conn.execute("SELECT * FROM reporting_regime ORDER BY sort_order"):
        node(row["regime_id"], "ReportingRegime", row["name"], "regime", row["regime_id"], dict(row))
        edge(estate_id, "HAS_REGIME", row["regime_id"])
    for row in conn.execute("SELECT * FROM reporting_collection ORDER BY sort_order"):
        node(row["collection_id"], "ReportingCollection", row["name"], "collection", row["collection_id"], dict(row))
        edge(row["regime_id"], "HAS_COLLECTION", row["collection_id"])
    for row in conn.execute(
        """SELECT r.*,c.regime_id,c.name AS collection_name,n.resolved_display_name,n.display_name_source
           FROM reporting_requirement r
           JOIN reporting_collection c ON c.collection_id=r.collection_id
           JOIN reporting_requirement_names n ON n.requirement_id=r.requirement_id"""
    ):
        node(row["requirement_id"], "ReportingRequirement", row["resolved_display_name"], "requirement", row["requirement_id"], dict(row))
        edge(row["requirement_id"], "BELONGS_TO_REGIME", row["regime_id"])
        edge(row["requirement_id"], "BELONGS_TO_COLLECTION", row["collection_id"])
    for row in conn.execute(
        """SELECT e.*,r.code,r.requirement_type,n.resolved_display_name,n.display_name_source
           FROM reporting_requirement_edition e
           JOIN reporting_requirement r ON r.requirement_id=e.requirement_id
           JOIN reporting_edition_names n ON n.edition_id=e.edition_id"""
    ):
        node(row["edition_id"], "RequirementEdition", row["resolved_display_name"], "edition", row["edition_id"], dict(row))
        edge(row["requirement_id"], "HAS_EDITION", row["edition_id"])
        if row["effective_from"]:
            previous = conn.execute(
                """SELECT edition_id FROM reporting_requirement_edition
                   WHERE requirement_id=? AND edition_id<>? AND coalesce(effective_from,'') < ?
                   ORDER BY effective_from DESC LIMIT 1""",
                (row["requirement_id"], row["edition_id"], row["effective_from"]),
            ).fetchone()
            if previous:
                edge(row["edition_id"], "SUPERSEDES", previous["edition_id"])
    for row in conn.execute("SELECT * FROM reporting_resource"):
        node(row["resource_id"], "ReportingResource", row["title"], "resource", row["resource_id"], dict(row))
    for row in conn.execute(
        """SELECT er.*,n.resolved_display_name,n.display_name_source,n.inherited_requirement_name
           FROM reporting_edition_resource er
           JOIN reporting_edition_resource_names n
             ON n.edition_id=er.edition_id AND n.resource_id=er.resource_id"""
    ):
        relationship = "HAS_TEMPLATE_RESOURCE" if row["relationship"] == "template" else "HAS_INSTRUCTION_RESOURCE" if row["relationship"] == "instructions" else "HAS_RESOURCE"
        edge(row["edition_id"], relationship, row["resource_id"], {
            "is_primary": bool(row["is_primary"]),
            "display_name": row["resolved_display_name"],
            "display_name_source": row["display_name_source"],
            "inherited_requirement_name": row["inherited_requirement_name"],
        })
    for row in conn.execute("SELECT * FROM reporting_taxonomy_release"):
        node(row["release_id"], "TaxonomyRelease", row["name"], "taxonomy_release", row["release_id"], dict(row))
    for row in conn.execute(
        """SELECT c.*,n.resolved_display_name,n.display_name_source
           FROM reporting_resource_component c
           JOIN reporting_component_names n ON n.component_id=c.component_id
           ORDER BY c.sort_order"""
    ):
        node_type = {
            "worksheet": "Worksheet",
            "logical_template": "LogicalTemplate",
            "instruction_section": "InstructionSection",
            "taxonomy_entry_point": "TaxonomyEntryPoint",
        }.get(row["component_type"], "ResourceComponent")
        properties = dict(row)
        properties["metadata"] = json.loads(properties.pop("metadata_json") or "{}")
        node(row["component_id"], node_type, row["resolved_display_name"], "component", row["component_id"], properties)
        if row["component_type"] == "worksheet":
            edge(row["resource_id"], "CONTAINS_SHEET", row["component_id"])
        elif row["component_type"] == "logical_template" and row["parent_component_id"]:
            edge(row["parent_component_id"], "IMPLEMENTS_TEMPLATE", row["component_id"])
        elif row["component_type"] == "instruction_section":
            edge(row["resource_id"], "CONTAINS_INSTRUCTION_SECTION", row["component_id"])
        elif row["component_type"] == "taxonomy_entry_point":
            metadata = properties.get("metadata") or {}
            release_id = metadata.get("release_id")
            requirement_id = metadata.get("requirement_id")
            if release_id:
                edge(release_id, "HAS_ENTRY_POINT", row["component_id"])
            if requirement_id:
                edge(row["component_id"], "ENCODES_REQUIREMENT", requirement_id)
    for row in conn.execute("SELECT * FROM reporting_taxonomy_resource"):
        edge(row["release_id"], "HAS_TAXONOMY_RESOURCE", row["resource_id"], {"role": row["relationship"]})
    for row in conn.execute("SELECT * FROM reporting_edition_taxonomy"):
        edge(row["edition_id"], "SUPPORTED_BY_TAXONOMY", row["release_id"], {"evidence": row["evidence"]})

    # Carry exact instruction-to-Rulebook references across to Resource nodes.
    for row in conn.execute(
        """SELECT DISTINCT r.resource_id,e.target_node_id,e.edge_type,e.evidence_span_id,e.confidence
           FROM reporting_resource r
           JOIN graph_node sd ON sd.node_type='SourceDocument' AND sd.source_pk=r.source_id
           JOIN graph_edge e ON e.source_node_id=sd.node_id
           WHERE e.edge_type IN ('REFERENCES_RULE','REFERENCES_SOURCE','REFERENCES_EXTERNAL')
             AND EXISTS (SELECT 1 FROM graph_node target WHERE target.node_id=e.target_node_id)"""
    ):
        edge(row["resource_id"], row["edge_type"], row["target_node_id"], {"legacy_evidence_span_id": row["evidence_span_id"]})
    return {"graph_nodes": nodes, "graph_edges": edges}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    conn = connect(args.db)
    try:
        print(json.dumps(rebuild(conn), indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
