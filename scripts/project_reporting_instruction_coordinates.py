#!/usr/bin/env python3
"""Project instruction provisions and explicit reporting coordinates into the graph.

The existing ``instruction`` table contains source-backed Annex XXV passages.
This projector creates one ``InstructionProvision`` graph node per passage,
links it to its source and default template, resolves deterministic legal
references, and creates ``INSTRUCTS`` edges only where the passage explicitly
names reporting rows/columns/cells.

Dry-run is read-only. ``--apply`` replaces only edges and nodes owned by this
projector inside one transaction.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import connect
from scripts.reporting_llm_reference_batch_api import (
    canonical_article_range_refs,
    canonical_article_refs,
    canonical_rule_refs,
    canonical_rulebook_structure_refs,
)

DB_PATH = ROOT / "backend" / "data" / "rulebook.sqlite3"
PROJECTOR = "instruction_coordinate_projection"
LEGAL_PROJECTOR = "instruction_legal_reference_projection"
SOURCE_TABLE = "instruction_projection"

TEMPLATE_CODE_RE = re.compile(r"\b([A-Z]{1,2})\s*(\d{1,3})\.(\d{1,2})\b", re.I)
BRACED_COORDINATE_RE = re.compile(
    r"\{\s*([A-Z]{1,2})\s*(\d{1,3})\.(\d{1,2})\s*;\s*"
    r"r(?:ow)?\s*(\d{2,5})(?:\s*;\s*c(?:ol(?:umn)?)?\s*(\d{2,5}))?\s*\}",
    re.I,
)
FOR_ROWS_COLUMN_RE = re.compile(
    r"\bfor\s+rows?\s+(.{1,360}?),\s*(?:credit\s+)?institutions?\s+"
    r"shall\s+report\s+in\s+columns?\s+(\d{2,5})\b",
    re.I | re.S,
)
REPORT_IN_ROW_RE = re.compile(
    r"\b(?:credit\s+)?institutions?\s+shall\s+report(?:\s+figure\s+from)?\s+"
    r"(?:in\s+)?row\s+(\d{2,5})\s+of\s+"
    r"(?:template\s+)?([A-Z]{1,2}\s*\d{1,3}\.\d{1,2})\b",
    re.I,
)
COLUMN_SPEC_RE = re.compile(
    r"\bcolumns?\s+((?:\d{2,5})(?:\s*(?:,|and|or|-)\s*\d{2,5})*)",
    re.I,
)


@dataclass(frozen=True)
class CoordinateMention:
    template_code: str
    row_spec: str
    column_spec: str
    relation: str
    evidence_text: str


def normalise_template_code(value: str | None) -> str:
    match = TEMPLATE_CODE_RE.search(value or "")
    if not match:
        return ""
    return f"{match.group(1).upper()}{int(match.group(2)):02d}.{int(match.group(3)):02d}"


def parse_instruction_coordinates(
    text: str,
    *,
    default_template_code: str = "",
) -> list[CoordinateMention]:
    """Return explicit coordinate mentions without inventing missing axes."""
    mentions: list[CoordinateMention] = []
    normative = bool(re.search(r"\bshall\s+report\b|\bis\s+to\s+be\s+reported\b", text, re.I))

    for match in BRACED_COORDINATE_RE.finditer(text):
        code = normalise_template_code("".join(match.group(1, 2)) + "." + match.group(3))
        relation = (
            "normative_reporting_coordinate"
            if normative and "for example" not in text[max(0, match.start() - 100) : match.end()].lower()
            else "explicit_coordinate_reference"
        )
        mentions.append(
            CoordinateMention(
                code,
                match.group(4),
                match.group(5) or "",
                relation,
                match.group(0),
            )
        )

    default_code = normalise_template_code(default_template_code)
    for match in FOR_ROWS_COLUMN_RE.finditer(text):
        code = default_code or normalise_template_code(match.group(0))
        if code:
            mentions.append(
                CoordinateMention(
                    code,
                    match.group(1),
                    match.group(2),
                    "normative_reporting_coordinate",
                    match.group(0),
                )
            )

    for match in REPORT_IN_ROW_RE.finditer(text):
        code = normalise_template_code(match.group(2))
        tail = text[match.end() :]
        # Stop at the next sentence-sized boundary; the source extraction often
        # uses bullets rather than full stops, so permit a generous window.
        tail = tail[:1200]
        column_specs = [m.group(1) for m in COLUMN_SPEC_RE.finditer(tail)]
        for column_spec in column_specs:
            mentions.append(
                CoordinateMention(
                    code,
                    match.group(1),
                    column_spec,
                    "normative_reporting_coordinate",
                    f"{match.group(0)}; column(s) {column_spec}",
                )
            )

    deduped: dict[tuple[str, str, str], CoordinateMention] = {}
    priority = {"explicit_coordinate_reference": 1, "normative_reporting_coordinate": 2}
    for mention in mentions:
        key = (mention.template_code, mention.row_spec, mention.column_spec)
        previous = deduped.get(key)
        if previous is None or priority[mention.relation] > priority[previous.relation]:
            deduped[key] = mention
    return list(deduped.values())


def expand_code_spec(spec: str, available_codes: Iterable[str]) -> list[str]:
    """Expand explicit numeric values/ranges only to codes present in the template."""
    available = sorted(
        {str(code) for code in available_codes if str(code).strip()},
        key=lambda value: (int(value) if value.isdigit() else 10**9, value),
    )
    by_number: dict[int, list[str]] = defaultdict(list)
    for code in available:
        if code.isdigit():
            by_number[int(code)].append(code)
    selected: set[str] = set()
    clean = (spec or "").replace("–", "-").replace("—", "-")
    for match in re.finditer(r"\b(\d{2,5})(?:\s*-\s*(\d{2,5}))?", clean):
        literal_start = match.group(1)
        literal_end = match.group(2)
        if literal_end is None and literal_start in available:
            # A few imported workbooks contain both ``0010`` and ``010`` as
            # column codes. An exact instruction token must not fan out to
            # both aliases.
            selected.add(literal_start)
            continue
        start = int(match.group(1))
        end = int(match.group(2) or match.group(1))
        lo, hi = sorted((start, end))
        for number, codes in by_number.items():
            if lo <= number <= hi:
                selected.update(codes)
    return [code for code in available if code in selected]


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _template_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.template_id,t.template_code,t.title,
               COUNT(DISTINCT d.datapoint_id) AS datapoints
        FROM template t
        LEFT JOIN datapoint d ON d.template_id=t.template_id
        GROUP BY t.template_id
        ORDER BY datapoints DESC,t.template_id
        """
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = normalise_template_code(row["template_code"])
        if code and code not in result:
            result[code] = dict(row)
    return result


def _coordinate_index(conn: sqlite3.Connection, template_id: str) -> dict[str, Any]:
    rows = {
        row["row_code"]: row["row_id"]
        for row in conn.execute(
            "SELECT row_id,row_code FROM template_row WHERE template_id=?",
            (template_id,),
        )
        if row["row_code"]
    }
    columns = {
        row["column_code"]: row["column_id"]
        for row in conn.execute(
            "SELECT column_id,column_code FROM template_column WHERE template_id=?",
            (template_id,),
        )
        if row["column_code"]
    }
    datapoints = {
        (row["row_code"], row["column_code"]): row["datapoint_id"]
        for row in conn.execute(
            """
            SELECT d.datapoint_id,tr.row_code,tc.column_code
            FROM datapoint d
            LEFT JOIN template_row tr ON tr.row_id=d.row_id
            LEFT JOIN template_column tc ON tc.column_id=d.column_id
            WHERE d.template_id=?
            """,
            (template_id,),
        )
        if row["row_code"] and row["column_code"]
    }
    return {"rows": rows, "columns": columns, "datapoints": datapoints}


def _canonical_source_node(conn: sqlite3.Connection, source_id: str) -> str:
    cleanup_exists = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='source_document_cleanup'
        """
    ).fetchone()
    row = (
        conn.execute(
            """
            SELECT canonical_source_id
            FROM source_document_cleanup
            WHERE source_id=?
            """,
            (source_id,),
        ).fetchone()
        if cleanup_exists
        else None
    )
    canonical = row["canonical_source_id"] if row else source_id
    return f"source_document:{canonical}"


def build_projection(conn: sqlite3.Connection) -> dict[str, Any]:
    templates = _template_index(conn)
    coordinate_indexes: dict[str, dict[str, Any]] = {}
    legal_targets = {
        row["canonical_key"]: dict(row)
        for row in conn.execute(
            """
            SELECT node_id,node_type,label,
                   json_extract(properties_json,'$.canonical_key') AS canonical_key
            FROM graph_node
            WHERE node_type='Provision'
              AND json_extract(properties_json,'$.canonical_key') IS NOT NULL
            """
        )
    }
    instructions = conn.execute(
        """
        SELECT i.*,sp.source_id,sp.page_number,sp.heading_path,sd.url AS source_url
        FROM instruction i
        LEFT JOIN source_span sp ON sp.span_id=i.source_span_id
        LEFT JOIN source_document sd ON sd.source_id=sp.source_id
        ORDER BY i.instruction_id
        """
    ).fetchall()

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    unresolved = Counter()
    coordinate_relations = Counter()

    def add_edge(
        source: str,
        target: str,
        edge_type: str,
        *,
        extraction_method: str,
        confidence: float,
        evidence_span_id: str | None,
        properties: dict[str, Any],
    ) -> None:
        edge_key = stable_id("edge", source, edge_type, target, extraction_method)
        existing = edges.get(edge_key)
        if existing and existing["properties"].get("coordinate_relation") == "normative_reporting_coordinate":
            return
        edges[edge_key] = {
            "edge_id": edge_key,
            "source_node_id": source,
            "target_node_id": target,
            "edge_type": edge_type,
            "properties": properties,
            "evidence_span_id": evidence_span_id,
            "confidence": confidence,
            "extraction_method": extraction_method,
            "review_status": "accepted_candidate",
        }

    for row in instructions:
        instruction_id = row["instruction_id"]
        node_id = f"instruction_provision:{instruction_id.removeprefix('instruction:')}"
        default_code = (
            normalise_template_code(row["applies_to_id"])
            if row["applies_to_type"] == "template"
            else ""
        )
        props = {
            "instruction_id": instruction_id,
            "instruction_set": row["instruction_set"],
            "applies_to_type": row["applies_to_type"],
            "applies_to_id": row["applies_to_id"],
            "text": row["text"],
            "description": row["text"],
            "source_span_id": row["source_span_id"],
            "source_id": row["source_id"],
            "source_url": row["source_url"],
            "page_number": row["page_number"],
            "heading_path": row["heading_path"],
        }
        nodes[node_id] = {
            "node_id": node_id,
            "node_type": "InstructionProvision",
            "label": (row["text"] or instruction_id)[:240],
            "source_table": SOURCE_TABLE,
            "source_pk": instruction_id,
            "properties": props,
            "review_status": "accepted_candidate",
        }

        if row["source_id"]:
            source_node = _canonical_source_node(conn, row["source_id"])
            if conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (source_node,)).fetchone():
                add_edge(
                    node_id,
                    source_node,
                    "EVIDENCED_BY",
                    extraction_method=PROJECTOR,
                    confidence=1.0,
                    evidence_span_id=row["source_span_id"],
                    properties={"evidence_status": "direct_text"},
                )

        if default_code and default_code in templates:
            template_id = templates[default_code]["template_id"]
            if conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (template_id,)).fetchone():
                add_edge(
                    node_id,
                    template_id,
                    "APPLIES_TO",
                    extraction_method=PROJECTOR,
                    confidence=1.0,
                    evidence_span_id=row["source_span_id"],
                    properties={
                        "evidence_status": "parsed_instruction_heading",
                        "template_code": default_code,
                    },
                )

        references = (
            canonical_article_range_refs(row["text"] or "")
            + canonical_article_refs(row["text"] or "")
            + canonical_rule_refs(row["text"] or "")
            + canonical_rulebook_structure_refs(row["text"] or "")
        )
        seen_legal: set[str] = set()
        for canonical_key, reference_label in references:
            if canonical_key in seen_legal:
                continue
            seen_legal.add(canonical_key)
            target = legal_targets.get(canonical_key)
            if not target:
                unresolved["legal_reference_target"] += 1
                continue
            add_edge(
                node_id,
                target["node_id"],
                "REFERENCES_RULE",
                extraction_method=LEGAL_PROJECTOR,
                confidence=0.96,
                evidence_span_id=row["source_span_id"],
                properties={
                    "evidence_status": "direct_text",
                    "canonical_key": canonical_key,
                    "reference_label": reference_label,
                },
            )

        mentions = parse_instruction_coordinates(
            row["text"] or "",
            default_template_code=default_code,
        )
        for mention in mentions:
            template = templates.get(mention.template_code)
            if not template:
                unresolved["template"] += 1
                continue
            template_id = template["template_id"]
            coordinate_index = coordinate_indexes.setdefault(
                template_id,
                _coordinate_index(conn, template_id),
            )
            row_codes = expand_code_spec(
                mention.row_spec,
                coordinate_index["rows"].keys(),
            )
            column_codes = (
                expand_code_spec(
                    mention.column_spec,
                    coordinate_index["columns"].keys(),
                )
                if mention.column_spec
                else []
            )
            if not row_codes:
                unresolved["row"] += 1
                continue
            targets: list[tuple[str, str, str]] = []
            if column_codes:
                for row_code in row_codes:
                    for column_code in column_codes:
                        datapoint_id = coordinate_index["datapoints"].get(
                            (row_code, column_code)
                        )
                        if datapoint_id:
                            targets.append((datapoint_id, row_code, column_code))
                        else:
                            unresolved["datapoint"] += 1
                            coordinate_id = stable_id(
                                "reporting_coordinate",
                                template_id,
                                row_code,
                                column_code,
                            )
                            nodes.setdefault(
                                coordinate_id,
                                {
                                    "node_id": coordinate_id,
                                    "node_type": "ReportingCoordinate",
                                    "label": (
                                        f"{mention.template_code} "
                                        f"r{row_code} c{column_code}"
                                    ),
                                    "source_table": SOURCE_TABLE,
                                    "source_pk": (
                                        f"{template_id}|r{row_code}|c{column_code}"
                                    ),
                                    "properties": {
                                        "template_id": template_id,
                                        "template_code": mention.template_code,
                                        "row_id": coordinate_index["rows"][row_code],
                                        "row_code": row_code,
                                        "column_id": coordinate_index["columns"][
                                            column_code
                                        ],
                                        "column_code": column_code,
                                        "materialized_datapoint_id": None,
                                        "coverage_status": (
                                            "instruction_defined_not_materialized"
                                        ),
                                    },
                                    "review_status": "accepted_candidate",
                                },
                            )
                            targets.append((coordinate_id, row_code, column_code))
            else:
                targets.extend(
                    (coordinate_index["rows"][row_code], row_code, "")
                    for row_code in row_codes
                )
            for target_id, row_code, column_code in targets:
                if target_id not in nodes and not conn.execute(
                    "SELECT 1 FROM graph_node WHERE node_id=?", (target_id,)
                ).fetchone():
                    unresolved["graph_node"] += 1
                    continue
                coordinate_relations[mention.relation] += 1
                add_edge(
                    node_id,
                    target_id,
                    "INSTRUCTS",
                    extraction_method=PROJECTOR,
                    confidence=(
                        0.99
                        if mention.relation == "normative_reporting_coordinate"
                        else 0.9
                    ),
                    evidence_span_id=row["source_span_id"],
                    properties={
                        "evidence_status": "direct_text",
                        "coordinate_relation": mention.relation,
                        "template_id": template_id,
                        "template_code": mention.template_code,
                        "row_code": row_code,
                        "column_code": column_code,
                        "coordinate_evidence": mention.evidence_text,
                    },
                )

    return {
        "nodes": nodes,
        "edges": edges,
        "unresolved": dict(unresolved),
        "coordinate_relations": dict(coordinate_relations),
    }


def project_instruction_coordinates(
    db_path: Path = DB_PATH,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    conn = connect(db_path)
    projection = build_projection(conn)
    edge_counts = Counter(
        edge["edge_type"] for edge in projection["edges"].values()
    )
    node_counts = Counter(
        node["node_type"] for node in projection["nodes"].values()
    )
    if apply:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM graph_edge WHERE extraction_method IN (?,?)",
                (PROJECTOR, LEGAL_PROJECTOR),
            )
            conn.execute(
                "DELETE FROM graph_node WHERE source_table=?",
                (SOURCE_TABLE,),
            )
            conn.executemany(
                """
                INSERT INTO graph_node(
                  node_id,node_type,label,source_table,source_pk,properties_json,
                  review_status
                ) VALUES (?,?,?,?,?,?,?)
                """,
                [
                    (
                        node["node_id"],
                        node["node_type"],
                        node["label"],
                        node["source_table"],
                        node["source_pk"],
                        json.dumps(node["properties"], ensure_ascii=False),
                        node["review_status"],
                    )
                    for node in projection["nodes"].values()
                ],
            )
            conn.executemany(
                """
                INSERT INTO graph_edge(
                  edge_id,source_node_id,target_node_id,edge_type,properties_json,
                  evidence_span_id,confidence,extraction_method,review_status
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        edge["edge_id"],
                        edge["source_node_id"],
                        edge["target_node_id"],
                        edge["edge_type"],
                        json.dumps(edge["properties"], ensure_ascii=False),
                        edge["evidence_span_id"],
                        edge["confidence"],
                        edge["extraction_method"],
                        edge["review_status"],
                    )
                    for edge in projection["edges"].values()
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    conn.close()
    return {
        "status": "applied" if apply else "dry_run",
        "nodes": len(projection["nodes"]),
        "edges": len(projection["edges"]),
        "nodes_by_type": dict(node_counts),
        "edges_by_type": dict(edge_counts),
        "coordinate_relations": projection["coordinate_relations"],
        "unresolved": projection["unresolved"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            project_instruction_coordinates(args.db, apply=args.apply),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
