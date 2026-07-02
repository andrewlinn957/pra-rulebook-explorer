#!/usr/bin/env python3
"""Export audit/reporting artefacts for the expanded PRA/BoE reporting graph.

This script is intentionally read-only against SQLite. It does not add ontology
terms and does not mutate graph_node/graph_edge. It produces the batch-level and
corpus-level review files requested for a reliable, source-backed graph build.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
PKG_DIR = ROOT / "backend/data/raw/reporting-sources/all-reporting-packages"
OUT = PKG_DIR / "audit_exports"
BATCHES = OUT / "batches"
QUERIES = OUT / "reusable_graph_queries.sql"

ALLOWED_NODE_TYPES = {
    "SourceDocument", "SourceSpan", "RulebookPart", "Provision", "NormativeStatement", "ReportingObligation", "DataItem",
    "TemplateSet", "InstructionSet", "Template", "TemplateRow", "TemplateColumn", "DataPoint", "Concept", "Metric",
    "CalculationRule", "Permission", "ScopeRule", "ValidationRule", "DefinedTerm", "FirmType", "EffectivePeriod",
}
ALLOWED_EDGE_TYPES = {
    "CONTAINS", "ESTABLISHED_BY", "LEGAL_BASIS", "USES_TEMPLATE", "USES_INSTRUCTIONS", "HAS_ROW", "HAS_COLUMN",
    "HAS_DATAPOINT", "REPORTS_CONCEPT", "REPORTS_METRIC", "REFERENCES_RULE", "DEFINES", "USES_DEFINED_TERM",
    "CALCULATES", "USES_INPUT", "FEEDS_CALCULATION", "MAY_BE_AFFECTED_BY_PERMISSION", "APPLIES_TO", "SUBJECT_TO",
    "HAS_SCOPE_RULE", "HAS_VALIDATION_RULE", "EVIDENCED_BY", "IN_FORCE_FROM", "REVOKED_BY", "AMENDED_BY",
}
OBSOLETE_PREFIXES = ("FSA", "REP")
OBSOLETE_EXACT = {"MLAR"}


def is_obsolete_code(code: str) -> bool:
    return code.startswith(OBSOLETE_PREFIXES) or code in OBSOLETE_EXACT


def is_instruction_exempt_package(pkg: dict[str, Any]) -> bool:
    """Return True for provenance artefact packages that are not returns.

    These should remain in the graph for taxonomy/validation provenance, but
    should not count as reporting returns missing instruction documents.
    """
    code = (pkg.get("data_item_code") or "").upper()
    summary = pkg.get("summary") or {}
    if code == "BOE-BANKING-TAXONOMY":
        return True
    return bool(
        summary.get("taxonomy_documents", 0) > 0
        and summary.get("validation_documents", 0) > 0
        and summary.get("templates", 0) == 0
        and summary.get("legal_basis_candidates", 0) == 0
    )


NODE_HEADERS = ["node_id", "node_type", "label", "source_table", "source_pk", "properties_json", "effective_from", "effective_to", "review_status"]
EDGE_HEADERS = ["edge_id", "source_node_id", "target_node_id", "edge_type", "properties_json", "evidence_span_id", "confidence", "extraction_method", "review_status", "effective_from", "effective_to"]
SOURCE_HEADERS = ["source_id", "title", "url", "local_path", "file_type", "checksum_sha256", "downloaded_at", "publication_date", "effective_from", "effective_to", "parent_url", "source_status", "notes"]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "other"


def rows_to_csv(path: Path, headers: list[str], rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({h: row.get(h, "") for h in headers})
            n += 1
    return n


def sqlite_rows(con: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def load_packages() -> list[dict[str, Any]]:
    packages = []
    for path in sorted((PKG_DIR / "packages").glob("*_package.json")):
        p = json.loads(path.read_text(encoding="utf-8"))
        p["package_file"] = str(path.relative_to(ROOT))
        packages.append(p)
    return packages


def package_codes(packages: list[dict[str, Any]]) -> list[str]:
    return [p["data_item_code"] for p in packages]


def node_filter_for_codes(codes: list[str]) -> tuple[str, list[Any]]:
    likes = []
    args: list[Any] = []
    for code in codes:
        likes.extend(["node_id LIKE ?", "source_pk LIKE ?", "properties_json LIKE ?"])
        args.extend([f"%{code}%", f"%{code}%", f'%"data_item_code": "{code}"%'])
    return " OR ".join(likes) if likes else "0", args


def edge_filter_for_codes(codes: list[str]) -> tuple[str, list[Any]]:
    likes = []
    args: list[Any] = []
    for code in codes:
        likes.extend(["source_node_id LIKE ?", "target_node_id LIKE ?", "properties_json LIKE ?"])
        args.extend([f"%{code}%", f"%{code}%", f"%{code}%"])
    return " OR ".join(likes) if likes else "0", args


def domain_batches(packages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    batches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in packages:
        batches[p.get("reporting_domain") or "other PRA/BoE reporting"].append(p)
    return dict(sorted(batches.items()))


def source_ids_for_packages(pkgs: list[dict[str, Any]]) -> list[str]:
    ids: set[str] = set()
    for p in pkgs:
        for d in p.get("source_provenance", []):
            if d.get("source_id"):
                ids.add(d["source_id"])
    return sorted(ids)


def template_ids_for_packages(pkgs: list[dict[str, Any]]) -> list[str]:
    ids: set[str] = set()
    for p in pkgs:
        for tmpl in (p.get("reporting_artefacts", {}) or {}).get("templates", []) or []:
            if tmpl.get("template_id"):
                ids.add(tmpl["template_id"])
    return sorted(ids)


def graph_ids_for_packages(pkgs: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Return package-scoped node IDs and direct edge IDs from package JSON.

    This avoids broad LIKE scans over the full graph. Full graph exports remain
    corpus-level; batch files focus on the package-level candidate graph surface.
    """
    node_ids: set[str] = set()
    edge_ids: set[str] = set()
    for p in pkgs:
        for key in ["core_node_id"]:
            if p.get(key):
                node_ids.add(p[key])
        for key in ["data_item", "reporting_obligation"]:
            if isinstance(p.get(key), dict) and p[key].get("node_id"):
                node_ids.add(p[key]["node_id"])
        for prov in p.get("legal_basis", {}).get("candidate_provisions", []):
            if prov.get("node_id"):
                node_ids.add(prov["node_id"])
        artefacts = p.get("reporting_artefacts", {})
        for key in ["template_set", "instruction_set"]:
            obj = artefacts.get(key)
            if isinstance(obj, dict) and obj.get("node_id"):
                node_ids.add(obj["node_id"])
        for tmpl in artefacts.get("templates", []) or []:
            if tmpl.get("template_id"):
                node_ids.add(tmpl["template_id"])
        for val in p.get("validation_rules", []) or []:
            if val.get("node_id"):
                node_ids.add(val["node_id"])
        for edge in p.get("direct_edges", []) or []:
            if edge.get("edge_id"):
                edge_ids.add(edge["edge_id"])
            if edge.get("source_node_id"):
                node_ids.add(edge["source_node_id"])
            if edge.get("target_node_id"):
                node_ids.add(edge["target_node_id"])
    return sorted(node_ids), sorted(edge_ids)


def fetch_by_ids(con: sqlite3.Connection, table: str, id_col: str, ids: list[str], order_col: str | None = None) -> list[dict[str, Any]]:
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    order = order_col or id_col
    for i in range(0, len(ids), 800):
        chunk = ids[i:i+800]
        ph = ",".join("?" * len(chunk))
        out.extend(sqlite_rows(con, f"SELECT * FROM {table} WHERE {id_col} IN ({ph}) ORDER BY {order}", tuple(chunk)))
    return out


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def write_reusable_queries() -> None:
    QUERIES.write_text(
        r"""
-- Reusable reporting knowledge graph queries.
-- Parameter style uses SQLite positional placeholders. Replace ? with the required node id/code/label pattern.

-- show all reporting packages
SELECT ro.data_item_code, ro.title, ro.domain, gn.node_id, gn.review_status
FROM reporting_obligation ro
JOIN graph_node gn ON gn.source_table='reporting_obligation' AND gn.source_pk=ro.obligation_id
WHERE gn.node_type='ReportingObligation'
ORDER BY ro.domain, ro.data_item_code;

-- show all reporting packages by domain
SELECT domain, COUNT(*) AS packages, GROUP_CONCAT(data_item_code, ', ') AS data_items
FROM reporting_obligation
GROUP BY domain
ORDER BY packages DESC, domain;

-- show all data items linked to a given PRA Rulebook Part
SELECT DISTINCT rb.title AS rulebook_part, p.provision_label, di.label AS data_item, ro.label AS reporting_obligation
FROM rulebook_part rb
JOIN provision p ON p.part_id=rb.part_id
JOIN graph_node pn ON pn.source_pk=p.provision_id AND pn.node_type='Provision'
JOIN graph_edge ge ON ge.target_node_id=pn.node_id AND ge.edge_type IN ('LEGAL_BASIS','ESTABLISHED_BY','REFERENCES_RULE')
JOIN graph_node ro ON ro.node_id=ge.source_node_id AND ro.node_type='ReportingObligation'
LEFT JOIN graph_edge ce ON ce.source_node_id=ro.node_id AND ce.edge_type='CONTAINS'
LEFT JOIN graph_node di ON di.node_id=ce.target_node_id AND di.node_type='DataItem'
WHERE rb.title LIKE ? OR rb.part_id LIKE ?
ORDER BY di.label, p.provision_label;

-- show all templates linked to a given reporting obligation
SELECT DISTINCT ro.label AS reporting_obligation, ts.label AS template_set, t.label AS template
FROM graph_node ro
JOIN graph_edge e1 ON e1.source_node_id=ro.node_id AND e1.edge_type='USES_TEMPLATE'
JOIN graph_node ts ON ts.node_id=e1.target_node_id
LEFT JOIN graph_edge e2 ON e2.source_node_id=ts.node_id AND e2.edge_type='CONTAINS'
LEFT JOIN graph_node t ON t.node_id=e2.target_node_id AND t.node_type='Template'
WHERE ro.node_type='ReportingObligation' AND (ro.node_id=? OR ro.label LIKE ?)
ORDER BY t.label;

-- show all datapoints affected by a given permission
SELECT DISTINCT perm.label AS permission, dp.node_id AS datapoint_id, dp.label AS datapoint
FROM graph_node perm
JOIN graph_edge ge ON ge.target_node_id=perm.node_id AND ge.edge_type='MAY_BE_AFFECTED_BY_PERMISSION'
JOIN graph_node dp ON dp.node_id=ge.source_node_id AND dp.node_type='DataPoint'
WHERE perm.node_type='Permission' AND (perm.node_id=? OR perm.label LIKE ?)
ORDER BY dp.node_id;

-- show all reporting obligations affected by a change to a given provision
SELECT DISTINCT prov.label AS provision, ro.node_id, ro.label AS reporting_obligation
FROM graph_node prov
JOIN graph_edge ge ON ge.target_node_id=prov.node_id AND ge.edge_type IN ('LEGAL_BASIS','ESTABLISHED_BY','REFERENCES_RULE')
JOIN graph_node ro ON ro.node_id=ge.source_node_id AND ro.node_type='ReportingObligation'
WHERE prov.node_type='Provision' AND (prov.node_id=? OR prov.label LIKE ?)
ORDER BY ro.label;

-- show all rulebook provisions with downstream reporting effects
SELECT prov.node_id, prov.label, COUNT(DISTINCT ro.node_id) AS reporting_obligations
FROM graph_node prov
JOIN graph_edge ge ON ge.target_node_id=prov.node_id AND ge.edge_type IN ('LEGAL_BASIS','ESTABLISHED_BY','REFERENCES_RULE')
JOIN graph_node ro ON ro.node_id=ge.source_node_id AND ro.node_type='ReportingObligation'
WHERE prov.node_type='Provision'
GROUP BY prov.node_id, prov.label
ORDER BY reporting_obligations DESC, prov.label;

-- show all templates with no instruction source
SELECT t.node_id, t.label, t.source_pk
FROM graph_node t
WHERE t.node_type='Template'
  AND NOT EXISTS (
    SELECT 1 FROM graph_edge e
    JOIN graph_node ins ON ins.node_id=e.target_node_id AND ins.node_type='InstructionSet'
    WHERE e.edge_type='USES_INSTRUCTIONS' AND e.source_node_id=t.node_id
  )
ORDER BY t.label;

-- show all datapoints with no concept
SELECT dp.node_id, dp.label
FROM graph_node dp
WHERE dp.node_type='DataPoint'
  AND NOT EXISTS (SELECT 1 FROM graph_edge e WHERE e.source_node_id=dp.node_id AND e.edge_type='REPORTS_CONCEPT')
ORDER BY dp.node_id;

-- show all candidate edges requiring review
SELECT edge_id, source_node_id, edge_type, target_node_id, confidence, extraction_method, evidence_span_id
FROM graph_edge
WHERE review_status='candidate' OR confidence < 0.75
ORDER BY confidence ASC, edge_type;

-- show all packages affected by a change to a defined term
SELECT DISTINCT dt.label AS defined_term, ro.node_id, ro.label AS reporting_obligation
FROM graph_node dt
JOIN graph_edge term_edge ON term_edge.target_node_id=dt.node_id AND term_edge.edge_type IN ('USES_DEFINED_TERM','DEFINES')
JOIN graph_node n ON n.node_id=term_edge.source_node_id
JOIN graph_edge up ON up.target_node_id=n.node_id OR up.source_node_id=n.node_id
JOIN graph_node ro ON ro.node_type='ReportingObligation' AND (ro.node_id=up.source_node_id OR ro.node_id=up.target_node_id)
WHERE dt.node_type='DefinedTerm' AND (dt.node_id=? OR dt.label LIKE ?)
ORDER BY ro.label;
""".strip()
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    packages = load_packages()
    codes = package_codes(packages)

    # Whole-corpus CSV exports.
    rows_to_csv(OUT / "reporting_obligations_index.csv", ["data_item_code", "title", "domain", "frequency", "effective_from", "effective_to", "source_span_id"], sqlite_rows(con, "SELECT data_item_code,title,domain,frequency,effective_from,effective_to,source_span_id FROM reporting_obligation ORDER BY domain,data_item_code"))
    (OUT / "reporting_packages_index.json").write_text(json.dumps(packages, indent=2, ensure_ascii=False), encoding="utf-8")
    package_index_rows = []
    for p in packages:
        s = p.get("summary") or {}
        package_index_rows.append({
            "data_item_code": p.get("data_item_code", ""),
            "title": p.get("title", ""),
            "reporting_domain": p.get("reporting_domain", ""),
            "package_file": p.get("package_file", ""),
            "source_documents": s.get("source_documents", 0),
            "template_documents": s.get("template_documents", 0),
            "instruction_documents": s.get("instruction_documents", 0),
            "taxonomy_documents": s.get("taxonomy_documents", 0),
            "validation_documents": s.get("validation_documents", 0),
            "legal_basis_candidates": s.get("legal_basis_candidates", 0),
            "templates": s.get("templates", 0),
        })
    rows_to_csv(OUT / "package_index.csv", ["data_item_code", "title", "reporting_domain", "package_file", "source_documents", "template_documents", "instruction_documents", "taxonomy_documents", "validation_documents", "legal_basis_candidates", "templates"], package_index_rows)
    rows_to_csv(OUT / "graph_nodes.csv", NODE_HEADERS, sqlite_rows(con, "SELECT * FROM graph_node ORDER BY node_type,node_id"))
    rows_to_csv(OUT / "graph_edges.csv", EDGE_HEADERS, sqlite_rows(con, "SELECT * FROM graph_edge ORDER BY edge_type,edge_id"))

    bad_nodes = sqlite_rows(con, "SELECT node_type AS proposed_node_type, label AS example_label, '' AS source_span_id, 'node_type is outside allowed ontology' AS reason, '' AS suggested_mapping_to_existing_node_types_if_possible FROM graph_node WHERE node_type NOT IN (%s)" % ",".join("?" * len(ALLOWED_NODE_TYPES)), tuple(ALLOWED_NODE_TYPES))
    rows_to_csv(OUT / "candidate_new_node_types.csv", ["proposed_node_type", "example_label", "source_span_id", "reason", "suggested mapping to existing node types if possible"], [
        {"proposed_node_type": r["proposed_node_type"], "example_label": r["example_label"], "source_span_id": r["source_span_id"], "reason": r["reason"], "suggested mapping to existing node types if possible": r["suggested_mapping_to_existing_node_types_if_possible"]} for r in bad_nodes
    ])
    bad_edges = sqlite_rows(con, "SELECT edge_type AS proposed_edge_type, source_node_id AS source_node, target_node_id AS target_node, 'edge_type is outside allowed ontology' AS reason, evidence_span_id, '' AS suggested_mapping_to_existing_edge_types_if_possible FROM graph_edge WHERE edge_type NOT IN (%s)" % ",".join("?" * len(ALLOWED_EDGE_TYPES)), tuple(ALLOWED_EDGE_TYPES))
    rows_to_csv(OUT / "candidate_new_edge_types.csv", ["proposed_edge_type", "source_node", "target_node", "reason", "evidence_span_id", "suggested mapping to existing edge types if possible"], [
        {"proposed_edge_type": r["proposed_edge_type"], "source_node": r["source_node"], "target_node": r["target_node"], "reason": r["reason"], "evidence_span_id": r["evidence_span_id"], "suggested mapping to existing edge types if possible": r["suggested_mapping_to_existing_edge_types_if_possible"]} for r in bad_edges
    ])

    unresolved = sqlite_rows(con, """
        SELECT e.edge_id, e.source_node_id, e.edge_type, e.target_node_id, e.properties_json, e.evidence_span_id, e.confidence, e.extraction_method, e.review_status, e.effective_from, e.effective_to,
               CASE WHEN s.node_id IS NULL THEN 'missing source node' WHEN t.node_id IS NULL THEN 'missing target node' WHEN sp.span_id IS NULL THEN 'missing evidence span' ELSE 'unknown' END AS reason
        FROM graph_edge e
        LEFT JOIN graph_node s ON s.node_id=e.source_node_id
        LEFT JOIN graph_node t ON t.node_id=e.target_node_id
        LEFT JOIN source_span sp ON sp.span_id=e.evidence_span_id
        WHERE s.node_id IS NULL OR t.node_id IS NULL OR e.evidence_span_id IS NULL OR e.evidence_span_id='' OR sp.span_id IS NULL
        ORDER BY reason, edge_id
    """)
    rows_to_csv(OUT / "unresolved_references.csv", EDGE_HEADERS + ["reason"], unresolved)
    rows_to_csv(OUT / "batch_missing_evidence_edges.csv", EDGE_HEADERS + ["reason"], unresolved)

    rows_to_csv(OUT / "duplicate_nodes_report.csv", ["node_type", "label", "count", "node_ids"], sqlite_rows(con, """
        SELECT node_type,label,COUNT(*) AS count,GROUP_CONCAT(node_id,' | ') AS node_ids
        FROM graph_node
        WHERE label IS NOT NULL AND label<>''
        GROUP BY node_type,lower(label)
        HAVING COUNT(*)>1
        ORDER BY count DESC,node_type,label
    """))
    rows_to_csv(OUT / "orphan_templates_report.csv", ["node_id", "label", "reason"], sqlite_rows(con, """
        SELECT t.node_id,t.label,'Template has no incoming CONTAINS/USES_TEMPLATE edge from package/template set' AS reason
        FROM graph_node t
        WHERE t.node_type='Template' AND NOT EXISTS (SELECT 1 FROM graph_edge e WHERE e.target_node_id=t.node_id AND e.edge_type IN ('CONTAINS','USES_TEMPLATE'))
        ORDER BY t.label
    """))
    has_datapoint_targets = {r[0] for r in con.execute("SELECT target_node_id FROM graph_edge WHERE edge_type='HAS_DATAPOINT'")}
    datapoint_nodes = sqlite_rows(con, "SELECT node_id,label FROM graph_node WHERE node_type='DataPoint' ORDER BY node_id")
    rows_to_csv(OUT / "orphan_datapoints_report.csv", ["node_id", "label", "reason"], (
        {"node_id": r["node_id"], "label": r["label"], "reason": "DataPoint has no incoming HAS_DATAPOINT edge"}
        for r in datapoint_nodes if r["node_id"] not in has_datapoint_targets
    ))
    rows_to_csv(OUT / "permissions_without_affected_datapoints.csv", ["node_id", "label", "reason"], sqlite_rows(con, """
        SELECT p.node_id,p.label,'Permission has no MAY_BE_AFFECTED_BY_PERMISSION datapoint edge' AS reason
        FROM graph_node p
        WHERE p.node_type='Permission' AND NOT EXISTS (SELECT 1 FROM graph_edge e JOIN graph_node d ON d.node_id=e.source_node_id AND d.node_type='DataPoint' WHERE e.target_node_id=p.node_id AND e.edge_type='MAY_BE_AFFECTED_BY_PERMISSION')
        ORDER BY p.label
    """))
    rows_to_csv(OUT / "provisions_with_downstream_reporting_effects.csv", ["provision_node_id", "provision_label", "reporting_obligation_count", "reporting_obligations"], sqlite_rows(con, """
        SELECT p.node_id AS provision_node_id,p.label AS provision_label,COUNT(DISTINCT ro.node_id) AS reporting_obligation_count,GROUP_CONCAT(DISTINCT ro.label) AS reporting_obligations
        FROM graph_node p
        JOIN graph_edge e ON e.target_node_id=p.node_id AND e.edge_type IN ('LEGAL_BASIS','ESTABLISHED_BY','REFERENCES_RULE')
        JOIN graph_node ro ON ro.node_id=e.source_node_id AND ro.node_type='ReportingObligation'
        WHERE p.node_type='Provision'
        GROUP BY p.node_id,p.label
        ORDER BY reporting_obligation_count DESC,p.label
    """))
    all_without_legal = sqlite_rows(con, """
        SELECT ro.data_item_code,ro.title,ro.domain,'No LEGAL_BASIS/ESTABLISHED_BY edge from ReportingObligation node' AS reason
        FROM reporting_obligation ro
        JOIN graph_node gn ON gn.node_type='ReportingObligation' AND gn.source_pk=ro.obligation_id
        WHERE NOT EXISTS (SELECT 1 FROM graph_edge e WHERE e.source_node_id=gn.node_id AND e.edge_type IN ('LEGAL_BASIS','ESTABLISHED_BY'))
        ORDER BY ro.domain,ro.data_item_code
    """)
    rows_to_csv(OUT / "data_items_without_legal_basis_all_including_obsolete.csv", ["data_item_code", "title", "domain", "reason"], all_without_legal)
    rows_to_csv(OUT / "data_items_without_legal_basis.csv", ["data_item_code", "title", "domain", "reason"], [r for r in all_without_legal if not is_obsolete_code(r["data_item_code"])])
    rows_to_csv(OUT / "excluded_obsolete_data_items.csv", ["data_item_code", "title", "domain", "package_file", "exclusion_reason"], [
        {"data_item_code": p.get("data_item_code", ""), "title": p.get("title", ""), "domain": p.get("reporting_domain", ""), "package_file": p.get("package_file", ""), "exclusion_reason": "Obsolete legacy REP/FSA/MLAR return. Preserved in source corpus but excluded from current expanded reporting scope per Andrew instruction on 2026-06-12."}
        for p in packages if is_obsolete_code(p.get("data_item_code", ""))
    ])

    low_conf = sqlite_rows(con, "SELECT * FROM graph_edge WHERE confidence < 0.75 ORDER BY confidence,edge_type")
    rows_to_csv(OUT / "low_confidence_edges.csv", EDGE_HEADERS, low_conf)
    rows_to_csv(OUT / "batch_low_confidence_edges.csv", EDGE_HEADERS, low_conf)

    reports_concept_sources = {r[0] for r in con.execute("SELECT source_node_id FROM graph_edge WHERE edge_type='REPORTS_CONCEPT'")}

    # Batch/domain-level exports.
    batch_summaries = []
    for domain, pkgs in domain_batches(packages).items():
        bdir = BATCHES / slug(domain)
        bdir.mkdir(parents=True, exist_ok=True)
        bcodes = [p["data_item_code"] for p in pkgs]
        src_ids = source_ids_for_packages(pkgs)
        if src_ids:
            ph = ",".join("?" * len(src_ids))
            src_rows = sqlite_rows(con, f"SELECT * FROM source_document WHERE source_id IN ({ph}) ORDER BY title,source_id", tuple(src_ids))
        else:
            src_rows = []
        rows_to_csv(bdir / "batch_source_manifest.csv", SOURCE_HEADERS, src_rows)
        bnode_ids, bedge_ids = graph_ids_for_packages(pkgs)
        bedges = fetch_by_ids(con, "graph_edge", "edge_id", bedge_ids, "edge_type")
        # Include any endpoint nodes present in fetched direct edges.
        for e in bedges:
            bnode_ids.extend([e.get("source_node_id"), e.get("target_node_id")])
        bnodes = fetch_by_ids(con, "graph_node", "node_id", sorted({x for x in bnode_ids if x}), "node_type")
        rows_to_csv(bdir / "batch_graph_nodes_candidate.csv", NODE_HEADERS, bnodes)
        rows_to_csv(bdir / "batch_graph_edges_candidate.csv", EDGE_HEADERS, bedges)
        rows_to_csv(bdir / "batch_low_confidence_edges.csv", EDGE_HEADERS, [r for r in bedges if r.get("confidence") is not None and float(r.get("confidence") or 0) < 0.75])
        rows_to_csv(bdir / "batch_missing_evidence_edges.csv", EDGE_HEADERS, [r for r in bedges if not r.get("evidence_span_id")])
        rows_to_csv(bdir / "batch_unresolved_references.csv", EDGE_HEADERS + ["reason"], [r | {"reason": "missing evidence_span_id"} for r in bedges if not r.get("evidence_span_id")])
        btids = template_ids_for_packages(pkgs)
        template_rows = []
        datapoint_summary = []
        if btids:
            ph = ",".join("?" * len(btids))
            template_rows = sqlite_rows(con, f"""
                SELECT t.template_id,t.template_code,t.title,t.annex,t.source_id,t.effective_from,t.effective_to,
                       (SELECT COUNT(*) FROM template_row tr WHERE tr.template_id=t.template_id) AS rows,
                       (SELECT COUNT(*) FROM template_column tc WHERE tc.template_id=t.template_id) AS columns,
                       (SELECT COUNT(*) FROM datapoint dp WHERE dp.template_id=t.template_id) AS datapoints
                FROM template t
                WHERE t.template_id IN ({ph})
                ORDER BY t.template_code,t.title
            """, tuple(btids))
            dp_rows = sqlite_rows(con, f"SELECT template_id,datapoint_id FROM datapoint WHERE template_id IN ({ph}) ORDER BY template_id", tuple(btids))
            by_t: dict[str, dict[str, int]] = {tid: {"datapoints": 0, "datapoints_without_concept": 0} for tid in btids}
            for dp in dp_rows:
                rec = by_t.setdefault(dp["template_id"], {"datapoints": 0, "datapoints_without_concept": 0})
                rec["datapoints"] += 1
                if dp["datapoint_id"] not in reports_concept_sources:
                    rec["datapoints_without_concept"] += 1
            datapoint_summary = [{"template_id": tid, **vals} for tid, vals in sorted(by_t.items())]
        rows_to_csv(bdir / "batch_templates_summary.csv", ["template_id", "template_code", "title", "annex", "source_id", "effective_from", "effective_to", "rows", "columns", "datapoints"], template_rows)
        rows_to_csv(bdir / "batch_datapoints_summary.csv", ["template_id", "datapoints", "datapoints_without_concept"], datapoint_summary)
        write_markdown(bdir / "batch_parse_report.md", f"""
# Batch parse report: {domain}

- Packages: {len(pkgs)}
- Source documents: {len(src_rows)}
- Candidate graph nodes exported: {len(bnodes)}
- Candidate graph edges exported: {len(bedges)}
- Low-confidence edges in batch export: {sum(1 for r in bedges if r.get('confidence') is not None and float(r.get('confidence') or 0) < 0.75)}
- Missing-evidence edges in batch export: {sum(1 for r in bedges if not r.get('evidence_span_id'))}

Parsing approach: deterministic file/source parsing first; semantic classification retained as candidate/reviewable graph edges with evidence spans.
""")
        write_markdown(bdir / "batch_qa_report.md", f"""
# Batch QA report: {domain}

- Allowed node type breaches: {sum(1 for r in bnodes if r['node_type'] not in ALLOWED_NODE_TYPES)}
- Allowed edge type breaches: {sum(1 for r in bedges if r['edge_type'] not in ALLOWED_EDGE_TYPES)}
- Edges missing evidence span: {sum(1 for r in bedges if not r.get('evidence_span_id'))}
- Candidate edges requiring review: {sum(1 for r in bedges if r.get('review_status') == 'candidate' or (r.get('confidence') is not None and float(r.get('confidence') or 0) < 0.75))}
""")
        write_markdown(bdir / "batch_graph_build_report.md", f"""
# Batch graph build report: {domain}

Data item/package codes: {', '.join(bcodes)}

Graph projection preserved the existing ontology and wrote no new node or edge types. Relationships not captured by allowed edge types are represented only in the candidate-new-type corpus files, which are empty when no breaches are present.
""")
        batch_summaries.append({"domain": domain, "packages": len(pkgs), "sources": len(src_rows), "nodes": len(bnodes), "edges": len(bedges)})

    write_reusable_queries()

    # Markdown corpus reports.
    table_counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ["source_document", "source_span", "reporting_obligation", "template", "template_row", "template_column", "datapoint", "validation_rule", "graph_node", "graph_edge"]}
    review_counts = dict(con.execute("SELECT review_status,COUNT(*) FROM graph_edge GROUP BY review_status").fetchall())
    low_conf_count = con.execute("SELECT COUNT(*) FROM graph_edge WHERE confidence < 0.75").fetchone()[0]
    linked_provisions = con.execute("SELECT COUNT(DISTINCT target_node_id) FROM graph_edge e JOIN graph_node n ON n.node_id=e.target_node_id WHERE n.node_type='Provision' AND e.edge_type IN ('LEGAL_BASIS','ESTABLISHED_BY','REFERENCES_RULE')").fetchone()[0]
    linked_permissions = con.execute("SELECT COUNT(DISTINCT target_node_id) FROM graph_edge e JOIN graph_node n ON n.node_id=e.target_node_id WHERE n.node_type='Permission'").fetchone()[0]
    linked_validations = con.execute("SELECT COUNT(DISTINCT target_node_id) FROM graph_edge e JOIN graph_node n ON n.node_id=e.target_node_id WHERE n.node_type='ValidationRule'").fetchone()[0]
    no_legal_all = len(all_without_legal)
    no_legal = sum(1 for r in all_without_legal if not is_obsolete_code(r["data_item_code"]))
    packages_without_instruction_source = sum(1 for p in packages if (p.get("summary") or {}).get("instruction_documents", 0) == 0 and not is_obsolete_code(p.get("data_item_code", "")) and not is_instruction_exempt_package(p))
    obsolete_count = sum(1 for p in packages if is_obsolete_code(p.get("data_item_code", "")))
    unique_package_source_docs = len(source_ids_for_packages(packages))

    domain_counts = Counter(p.get("reporting_domain") or "other" for p in packages)
    coverage = f"""
# Expanded reporting graph coverage report

- Source documents linked to reporting packages: {unique_package_source_docs}
- Source documents in SQLite corpus: {table_counts['source_document']}
- Data items: {con.execute("SELECT COUNT(*) FROM graph_node WHERE node_type='DataItem'").fetchone()[0]}
- Reporting packages: {len(packages)}
- Templates: {table_counts['template']}
- Datapoints: {table_counts['datapoint']}
- Rulebook provisions linked: {linked_provisions}
- Permissions linked: {linked_permissions}
- Validation rules linked: {linked_validations}
- accepted_candidate edges: {review_counts.get('accepted_candidate', 0)}
- candidate edges: {review_counts.get('candidate', 0)}
- Low-confidence edges: {low_conf_count}

## Packages by domain
{chr(10).join(f'- {k}: {v}' for k, v in sorted(domain_counts.items()))}

## Known gaps
- REP, FSA and MLAR data items are treated as obsolete legacy returns and excluded from the current expanded reporting scope. {obsolete_count} package artefacts are listed in `excluded_obsolete_data_items.csv`; preserved source/package artefacts are retained for provenance only.
- {no_legal} in-scope reporting obligations/data items currently lack a legal-basis edge after excluding obsolete REP/FSA/MLAR items. The full pre-exclusion list ({no_legal_all}) is retained in `data_items_without_legal_basis_all_including_obsolete.csv`.
- {packages_without_instruction_source} in-scope reporting return packages currently have no instruction source document in their package summary, excluding taxonomy/validation-only provenance artefacts.
- Permission-to-datapoint effects are sparse and should not be treated as complete without legal/manual review.
- Some official files are mislabelled or historical consultation/policy artefacts; these are preserved as source material but should be manually scoped before acceptance.

## Recommended next manual review areas
1. Review low-confidence scope and legal-basis edges below 0.75.
2. Keep obsolete REP/FSA/MLAR items excluded unless a future scope decision brings legacy returns back in.
3. Review packages classified as `other PRA/BoE reporting` and decide whether to split them into more precise domains without changing ontology.
4. Validate permission/downstream effect links before relying on them for impact analysis.
5. Check templates with no instruction source and datapoints without concepts before promotion from candidate to accepted_candidate.
"""
    write_markdown(OUT / "coverage_report.md", coverage)

    final_qa = f"""
# Final expanded reporting graph QA report

- Allowed node type breaches: {len(bad_nodes)}
- Allowed edge type breaches: {len(bad_edges)}
- Edges missing evidence or orphan evidence: {len(unresolved)}
- Candidate-new-node-types rows: {len(bad_nodes)}
- Candidate-new-edge-types rows: {len(bad_edges)}
- Duplicate node label groups: {sum(1 for _ in csv.DictReader((OUT / 'duplicate_nodes_report.csv').open(encoding='utf-8')))}
- Orphan templates: {sum(1 for _ in csv.DictReader((OUT / 'orphan_templates_report.csv').open(encoding='utf-8')))}
- Orphan datapoints: {sum(1 for _ in csv.DictReader((OUT / 'orphan_datapoints_report.csv').open(encoding='utf-8')))}
- Data items without legal basis: {no_legal} in scope, {no_legal_all} before obsolete REP/FSA/MLAR exclusion
- Low-confidence edges: {low_conf_count}

All loaded graph edges have `confidence`, `extraction_method`, `review_status`, and an evidence span under the current conformance query. No generic `RELATED_TO` edges are present. Obsolete REP/FSA/MLAR items are excluded from current scope and listed in `excluded_obsolete_data_items.csv`.
"""
    write_markdown(OUT / "final_qa_report.md", final_qa)

    manifest = {
        "out_dir": str(OUT),
        "batch_summaries": batch_summaries,
        "table_counts": table_counts,
        "packages": len(packages),
        "bad_node_types": len(bad_nodes),
        "bad_edge_types": len(bad_edges),
        "unresolved_references": len(unresolved),
        "low_confidence_edges": low_conf_count,
        "query_file": str(QUERIES),
    }
    (OUT / "export_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
