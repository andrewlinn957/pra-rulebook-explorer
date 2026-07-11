#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
OUT = ROOT / "backend/data/raw/reporting-sources/cor011-lcr-final/semantic-extraction"
MODEL = os.environ.get("PRA_REPORTING_SEMANTIC_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4.1-nano"
PROMPT_VERSION = "cor011-semantic-candidates-v1"

ALLOWED_NODE_TYPES = {
    "SourceDocument","SourceSpan","RulebookPart","Provision","NormativeStatement","ReportingObligation","DataItem",
    "TemplateSet","InstructionSet","Template","TemplateRow","TemplateColumn","DataPoint","Concept","Metric",
    "CalculationRule","Permission","ScopeRule","ValidationRule","DefinedTerm","FirmType","EffectivePeriod",
}
ALLOWED_EDGE_TYPES = {
    "CONTAINS","ESTABLISHED_BY","LEGAL_BASIS","USES_TEMPLATE","USES_INSTRUCTIONS","HAS_ROW","HAS_COLUMN",
    "HAS_DATAPOINT","REPORTS_CONCEPT","REPORTS_METRIC","REFERENCES_RULE","DEFINES","USES_DEFINED_TERM",
    "CALCULATES","USES_INPUT","FEEDS_CALCULATION","MAY_BE_AFFECTED_BY_PERMISSION","APPLIES_TO","SUBJECT_TO",
    "HAS_SCOPE_RULE","HAS_VALIDATION_RULE","EVIDENCED_BY","IN_FORCE_FROM","REVOKED_BY","AMENDED_BY",
}

@dataclass
class Node:
    node_id: str
    node_type: str
    label: str
    source_table: str = ""
    source_pk: str = ""
    properties_json: str = "{}"
    effective_from: str = ""
    effective_to: str = ""
    review_status: str = "candidate"

@dataclass
class Edge:
    source_node_id: str
    edge_type: str
    target_node_id: str
    evidence_span_id: str
    confidence: float
    extraction_method: str
    review_status: str
    explanation: str


def h(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]

class Extractor:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(DB)
        self.conn.row_factory = sqlite3.Row
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        self.low: list[Edge] = []
        self.missing: list[Edge] = []
        self.llm_calls = 0
        self.llm_edges = 0
        self.llm_errors: list[str] = []

    def add_node(self, node_id: str, node_type: str, label: str, **kw: Any) -> None:
        assert node_type in ALLOWED_NODE_TYPES, node_type
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(node_id=node_id, node_type=node_type, label=label, **kw)

    def add_edge(self, src: str, typ: str, tgt: str, span: str | None, conf: float, method: str, status: str, explanation: str) -> None:
        assert typ in ALLOWED_EDGE_TYPES, typ
        explanation = re.sub(r"\s+", " ", explanation).strip()[:180]
        e = Edge(src, typ, tgt, span or "", max(0.0, min(1.0, conf)), method, status, explanation)
        key = h(src, typ, tgt, span or "", method)
        if not span:
            self.missing.append(e)
            return
        if e.confidence < 0.75:
            self.low.append(e)
        self.edges.setdefault(key, e)

    def best_span(self, *terms: str, source_id: str | None = None, span_type: str | None = None) -> str | None:
        clauses = []
        params: list[Any] = []
        if source_id:
            clauses.append("source_id=?"); params.append(source_id)
        if span_type:
            clauses.append("span_type=?"); params.append(span_type)
        for t in terms:
            clauses.append("normalised_text LIKE ?"); params.append(f"%{t}%")
        where = " AND ".join(clauses) if clauses else "1=1"
        row = self.conn.execute(f"SELECT span_id FROM source_span WHERE {where} ORDER BY length(COALESCE(normalised_text,'')) LIMIT 1", params).fetchone()
        if row: return row[0]
        # relax to any term
        if len(terms) > 1:
            for t in terms:
                row = self.conn.execute("SELECT span_id FROM source_span WHERE normalised_text LIKE ? ORDER BY length(COALESCE(normalised_text,'')) LIMIT 1", (f"%{t}%",)).fetchone()
                if row: return row[0]
        return None

    def span_text(self, span_id: str) -> str:
        r = self.conn.execute("SELECT normalised_text FROM source_span WHERE span_id=?", (span_id,)).fetchone()
        return r[0] if r else ""

    def seed_nodes(self) -> None:
        self.add_node("data_item:COR011", "DataItem", "COR011", source_table="reporting_obligation", source_pk="data_item:COR011")
        self.add_node("reporting_obligation:COR011", "ReportingObligation", "COR011 reporting obligation")
        self.add_node("metric:LiquidityCoverageRatio", "Metric", "Liquidity Coverage Ratio", source_table="concept", source_pk="concept:LiquidityCoverageRatio")
        self.add_node("template_set:AnnexXXIV", "TemplateSet", "Annex XXIV reporting templates")
        self.add_node("instruction_set:AnnexXXV", "InstructionSet", "Annex XXV instructions")
        for pid, label in [
            ("provision:ReportingCRR.Article430.1.d", "Reporting (CRR) Article 430(1)(d)"),
            ("provision:ReportingCRR.Article16", "Reporting (CRR) Article 16"),
            ("provision:LCR.Article4", "Liquidity Coverage Ratio (CRR) Article 4"),
            ("provision:LCR.Article17", "Liquidity Coverage Ratio (CRR) Article 17"),
            ("provision:LCR.Article33", "Liquidity Coverage Ratio (CRR) Article 33"),
            ("provision:LCR.Article34", "Liquidity Coverage Ratio (CRR) Article 34"),
            ("provision:LiquidityCRR.Article415", "Liquidity (CRR) Article 415"),
        ]:
            self.add_node(pid, "Provision", label)
        for code, title in self.conn.execute("SELECT template_code,title FROM template ORDER BY template_code"):
            self.add_node(f"template:{code}", "Template", f"{code} {title}", source_table="template", source_pk=f"template:{code}")
        for cid, label in [
            ("concept:LiquidityCoverageRatio", "liquidity coverage ratio"),
            ("concept:LiquidityBuffer", "liquidity buffer"),
            ("concept:NetLiquidityOutflows", "net liquidity outflows"),
            ("concept:LiquidAssets", "liquid assets"),
            ("concept:Level1Assets", "Level 1 assets"),
            ("concept:Level2AAssets", "Level 2A assets"),
            ("concept:Level2BAssets", "Level 2B assets"),
            ("concept:Outflows", "outflows"),
            ("concept:Inflows", "inflows"),
            ("concept:InflowCap", "inflow cap"),
            ("concept:CollateralSwaps", "collateral swaps"),
            ("concept:ConsolidationPerimeter", "consolidation perimeter"),
        ]:
            self.add_node(cid, "Concept", label)
        self.add_node("calculation:LCRFormula", "CalculationRule", "LCR = liquidity buffer / net liquidity outflows")
        for pid, label in [
            ("permission:LCR.Article17.4", "Article 17 permission affecting liquidity buffer composition"),
            ("permission:LCR.Article33", "Article 33 inflow cap permission"),
            ("permission:LCR.Article34", "Article 34 higher group inflow rate permission"),
        ]:
            self.add_node(pid, "Permission", label, source_table="permission", source_pk=pid)
        for sid, label in [
            ("scope_rule:CRRFirm", "CRR firm"),
            ("scope_rule:CRRConsolidationEntity", "CRR consolidation entity"),
            ("scope_rule:IndividualBasis", "individual basis"),
            ("scope_rule:ConsolidatedBasis", "consolidated basis"),
            ("scope_rule:SubConsolidatedBasis", "sub-consolidated basis"),
        ]:
            self.add_node(sid, "ScopeRule", label)

    def add_template_nodes_edges(self) -> None:
        ann_span = self.best_span("Annex XXIV", "Template C 72.00") or self.best_span("Annex XXIV")
        instr_span = self.best_span("Annex XXV", "instructions") or self.best_span("Annex XXV")
        if instr_span:
            instr_source = self.conn.execute(
                """
                SELECT d.source_id,d.title
                FROM source_span s
                JOIN source_document d ON d.source_id=s.source_id
                WHERE s.span_id=?
                """,
                (instr_span,),
            ).fetchone()
            if instr_source:
                source_node = f"source_document:{instr_source['source_id']}"
                self.add_node(source_node, "SourceDocument", instr_source["title"] or instr_source["source_id"], source_table="source_document", source_pk=instr_source["source_id"])
                self.add_edge("instruction_set:AnnexXXV", "EVIDENCED_BY", source_node, instr_span, 1.0, "deterministic", "accepted_candidate", "Annex XXV instruction set is evidenced by the official Annex XXV instructions PDF.")
        self.add_edge("data_item:COR011", "USES_TEMPLATE", "template_set:AnnexXXIV", ann_span, 0.98, "deterministic", "accepted_candidate", "Annex XXIV is the reporting template set for liquidity reporting.")
        self.add_edge("data_item:COR011", "USES_INSTRUCTIONS", "instruction_set:AnnexXXV", instr_span, 0.98, "deterministic", "accepted_candidate", "Annex XXV provides instructions for liquidity reporting.")
        for code, concept, term in [
            ("C72.00", "concept:LiquidAssets", "C 72.00"),
            ("C73.00", "concept:Outflows", "C 73.00"),
            ("C74.00", "concept:Inflows", "C 74.00"),
            ("C75.01", "concept:CollateralSwaps", "C 75.01"),
            ("C76.00", "metric:LiquidityCoverageRatio", "C 76.00"),
            ("PERIMETER_OF_CONSOLIDATION", "concept:ConsolidationPerimeter", "perimeter"),
        ]:
            tid = f"template:{code}"
            span = self.best_span(term, source_id="b84391da5c1f5445") or ann_span
            self.add_edge("template_set:AnnexXXIV", "CONTAINS", tid, span, 0.99, "deterministic", "accepted_candidate", f"Annex XXIV contains template {code}.")
            self.add_edge("data_item:COR011", "USES_TEMPLATE", tid, span, 0.95, "deterministic", "accepted_candidate", f"COR011 uses template {code}.")
            self.add_edge(tid, "USES_INSTRUCTIONS", "instruction_set:AnnexXXV", instr_span, 0.93, "deterministic", "accepted_candidate", f"Template {code} uses Annex XXV instructions.")
            self.add_edge(tid, "REPORTS_CONCEPT" if concept.startswith("concept:") else "REPORTS_METRIC", concept, span, 0.86, "semantic_regex", "candidate", f"Template title maps {code} to the reported concept.")
        # Add row, column, datapoint nodes and structural edges, all evidenced by their source spans where available.
        for r in self.conn.execute("SELECT row_id,template_id,row_code,label,source_span_id FROM template_row"):
            nid = r["row_id"]
            self.add_node(nid, "TemplateRow", f"{r['template_id']} row {r['row_code']} {r['label'] or ''}", source_table="template_row", source_pk=nid)
            span = r["source_span_id"] or self.best_span(r["row_code"] or "", source_id="b84391da5c1f5445") or ann_span
            self.add_edge(r["template_id"], "HAS_ROW", nid, span, 0.99, "deterministic", "accepted_candidate", "Row is parsed from the Annex XXIV workbook.")
        for r in self.conn.execute("SELECT column_id,template_id,column_code,label,source_span_id FROM template_column"):
            nid = r["column_id"]
            self.add_node(nid, "TemplateColumn", f"{r['template_id']} column {r['column_code']} {r['label'] or ''}", source_table="template_column", source_pk=nid)
            span = r["source_span_id"] or self.best_span(r["column_code"] or "", source_id="b84391da5c1f5445") or ann_span
            self.add_edge(r["template_id"], "HAS_COLUMN", nid, span, 0.99, "deterministic", "accepted_candidate", "Column is parsed from the Annex XXIV workbook.")
        for r in self.conn.execute("SELECT datapoint_id,template_id,row_id,column_id,concept_label,source_span_id FROM datapoint"):
            nid = r["datapoint_id"]
            self.add_node(nid, "DataPoint", r["concept_label"] or nid, source_table="datapoint", source_pk=nid)
            span = r["source_span_id"] or self.best_span((r["concept_label"] or "")[:60], source_id="b84391da5c1f5445") or ann_span
            self.add_edge(r["template_id"], "HAS_DATAPOINT", nid, span, 0.98, "deterministic", "accepted_candidate", "Datapoint is parsed from row and column intersection.")
            label = (r["concept_label"] or "").lower()
            for term, cid in [("asset", "concept:LiquidAssets"),("outflow","concept:Outflows"),("inflow","concept:Inflows"),("collateral","concept:CollateralSwaps")]:
                if term in label:
                    self.add_edge(nid, "REPORTS_CONCEPT", cid, span, 0.76, "semantic_regex", "candidate", "Datapoint label contains the concept term.")
                    break

    def add_legal_concept_edges(self) -> None:
        s430 = self.best_span("Article 430(1)", "Liquidity Coverage Ratio") or self.best_span("Article 430", "Liquidity Coverage Ratio")
        s16 = self.best_span("Article 16 Reporting on Liquidity Coverage Requirement")
        s4 = self.best_span("ratio of a credit institution", "liquidity buffer", "net liquidity outflows")
        s17 = self.best_span("Article 17", "liquidity buffer") or self.best_span("Article 17(4)")
        s33 = self.best_span("Article 33", "inflow cap")
        s34 = self.best_span("higher inflow rate") or self.best_span("Article 34 Inflows Within a Group")
        s415 = self.best_span("Article 415") or self.best_span("Liquidity (CRR)", "Article 415")
        self.add_edge("reporting_obligation:COR011", "ESTABLISHED_BY", "provision:ReportingCRR.Article430.1.d", s430, 0.98, "deterministic", "accepted_candidate", "Article 430(1)(d) is cited for LCR reporting compliance.")
        self.add_edge("reporting_obligation:COR011", "LEGAL_BASIS", "provision:ReportingCRR.Article16", s16, 0.96, "deterministic", "accepted_candidate", "Article 16 is titled reporting on liquidity coverage requirement.")
        self.add_edge("provision:ReportingCRR.Article430.1.d", "REFERENCES_RULE", "provision:LCR.Article4", s430, 0.84, "semantic_regex", "candidate", "Article 430(1)(d) references the LCR Part basis.")
        self.add_edge("provision:ReportingCRR.Article430.1.d", "REFERENCES_RULE", "provision:LiquidityCRR.Article415", s415 or s430, 0.72, "semantic_regex", "candidate", "Liquidity CRR Article 415 is imported where relevant.")
        self.add_edge("calculation:LCRFormula", "CALCULATES", "metric:LiquidityCoverageRatio", s4, 0.99, "deterministic", "accepted_candidate", "Article 4 states the LCR ratio formula.")
        self.add_edge("calculation:LCRFormula", "USES_INPUT", "concept:LiquidityBuffer", s4, 0.99, "deterministic", "accepted_candidate", "LCR formula uses liquidity buffer as numerator.")
        self.add_edge("calculation:LCRFormula", "USES_INPUT", "concept:NetLiquidityOutflows", s4, 0.99, "deterministic", "accepted_candidate", "LCR formula uses net liquidity outflows as denominator.")
        for tpl in ["template:C72.00", "template:C73.00", "template:C74.00", "template:C75.01"]:
            span = self.best_span(tpl.split(":",1)[1].replace("C", "C "), source_id="b84391da5c1f5445") or s4
            self.add_edge(tpl, "FEEDS_CALCULATION", "template:C76.00", span, 0.80, "semantic_regex", "candidate", "Template is part of the LCR calculation workbook sequence.")
        for perm, prov, span, expl in [
            ("permission:LCR.Article17.4", "provision:LCR.Article17", s17, "Article 17 permission may affect liquidity buffer composition."),
            ("permission:LCR.Article33", "provision:LCR.Article33", s33, "Article 33 relates to inflow cap treatment."),
            ("permission:LCR.Article34", "provision:LCR.Article34", s34, "Article 34 permits higher group inflow rates."),
        ]:
            self.add_edge("reporting_obligation:COR011", "MAY_BE_AFFECTED_BY_PERMISSION", perm, span, 0.82, "semantic_regex", "candidate", expl)
            self.add_edge(perm, "LEGAL_BASIS", prov, span, 0.90, "deterministic", "accepted_candidate", "Permission hook is tied to the cited LCR Article.")
        for perm, targets, span in [
            ("permission:LCR.Article17.4", ["template:C72.00", "template:C76.00"], s17),
            ("permission:LCR.Article33", ["template:C74.00", "template:C76.00"], s33),
            ("permission:LCR.Article34", ["template:C74.00", "template:C76.00"], s34),
        ]:
            for target in targets:
                self.add_edge(target, "MAY_BE_AFFECTED_BY_PERMISSION", perm, span, 0.80, "semantic_regex", "candidate", "Permission may affect this LCR reporting template area.")
        for perm, template_id, span in [("permission:LCR.Article17.4","template:C72.00",s17),("permission:LCR.Article33","template:C74.00",s33),("permission:LCR.Article34","template:C74.00",s34)]:
            for dp in self.conn.execute("SELECT datapoint_id FROM datapoint WHERE template_id=? LIMIT 25", (template_id,)):
                self.add_edge(dp["datapoint_id"], "MAY_BE_AFFECTED_BY_PERMISSION", perm, span, 0.76, "semantic_regex", "candidate", "Representative datapoint in affected template area.")
        for term, cid in [("liquidity buffer","concept:LiquidityBuffer"),("net liquidity outflows","concept:NetLiquidityOutflows"),("level 1 assets","concept:Level1Assets"),("Level 2A assets","concept:Level2AAssets"),("Level 2B assets","concept:Level2BAssets")]:
            span = self.best_span(term)
            self.add_edge("provision:LCR.Article4", "DEFINES", cid, span, 0.78, "semantic_regex", "candidate", f"LCR Part span defines or uses {term}.")
        for sid, term in [("scope_rule:CRRFirm","CRR Firms"),("scope_rule:CRRConsolidationEntity","CRR consolidation entity"),("scope_rule:IndividualBasis","individual basis"),("scope_rule:ConsolidatedBasis","consolidated basis"),("scope_rule:SubConsolidatedBasis","sub-consolidated basis")]:
            span = self.best_span(term)
            self.add_edge("reporting_obligation:COR011", "HAS_SCOPE_RULE", sid, span, 0.80, "semantic_regex", "candidate", f"Scope term appears in relevant reporting/LCR materials: {term}.")
            self.add_edge(sid, "APPLIES_TO", "data_item:COR011", span, 0.74, "semantic_regex", "candidate", f"Scope term may apply to COR011 reporting.")

    def add_validation_edges(self) -> None:
        for r in self.conn.execute("SELECT validation_id,label,source_span_id,source_id FROM validation_rule LIMIT 500"):
            vid = r["validation_id"]
            self.add_node(vid, "ValidationRule", r["label"] or vid, source_table="validation_rule", source_pk=vid)
            span = r["source_span_id"] or self.best_span("validation", source_id=r["source_id"])
            self.add_edge("data_item:COR011", "HAS_VALIDATION_RULE", vid, span, 0.70, "semantic_regex", "candidate", "Validation artifact was selected from relevant taxonomy validation material.")

    def llm_span_ids(self) -> list[str]:
        terms = [
            ("Article 430(1)", "Liquidity Coverage Ratio"), ("Article 16 Reporting",),
            ("ratio of a credit institution", "liquidity buffer", "net liquidity outflows"),
            ("higher inflow rate",), ("inflow cap",), ("Level 1 assets",), ("Level 2A assets",), ("Level 2B assets",),
            ("C 76.00",), ("C 72.00",), ("C 73.00",), ("C 74.00",), ("C 75.01",),
            ("CRR consolidation entity",), ("sub-consolidated basis",)
        ]
        out=[]
        for ts in terms:
            sid=self.best_span(*ts)
            if sid and sid not in out: out.append(sid)
        return out[:18]

    def run_llm(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            self.llm_errors.append("OPENAI_API_KEY not set; LLM pass skipped")
            return
        known_nodes = [{"id": n.node_id, "type": n.node_type, "label": n.label} for n in self.nodes.values() if n.node_type in {"Provision","ReportingObligation","DataItem","TemplateSet","InstructionSet","Template","Concept","Metric","CalculationRule","Permission","ScopeRule"}]
        system = "You extract candidate semantic graph edges for COR011. Return JSON only. Do not invent facts. Every edge must be directly supported by the provided span. Use only supplied node IDs and allowed edge types."
        for sid in self.llm_span_ids():
            txt = self.span_text(sid)[:3000]
            user = {
                "task": "Return candidate edges supported by this single evidence span.",
                "evidence_span_id": sid,
                "allowed_edge_types": sorted(ALLOWED_EDGE_TYPES),
                "known_nodes": known_nodes,
                "span_text": txt,
                "json_shape": {"edges":[{"source_node_id":"known id","edge_type":"allowed type","target_node_id":"known id","confidence":0.0,"explanation":"<=30 words"}]},
            }
            try:
                self.llm_calls += 1
                resp = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": MODEL, "messages":[{"role":"system","content":system},{"role":"user","content":json.dumps(user)}], "temperature":0, "response_format":{"type":"json_object"}},
                    timeout=60,
                )
                if resp.status_code >= 400:
                    self.llm_errors.append(f"{sid}: HTTP {resp.status_code} {resp.text[:200]}")
                    continue
                content = resp.json()["choices"][0]["message"]["content"]
                data = json.loads(content)
                for e in data.get("edges", []):
                    src, typ, tgt = e.get("source_node_id"), e.get("edge_type"), e.get("target_node_id")
                    if src not in self.nodes or tgt not in self.nodes or typ not in ALLOWED_EDGE_TYPES:
                        continue
                    conf = float(e.get("confidence") or 0)
                    status = "accepted_candidate" if conf >= 0.90 else "candidate"
                    self.add_edge(src, typ, tgt, sid, conf, f"llm:{MODEL}:{PROMPT_VERSION}", status, e.get("explanation") or "LLM semantic candidate from cited span.")
                    self.llm_edges += 1
                time.sleep(0.2)
            except Exception as ex:
                self.llm_errors.append(f"{sid}: {type(ex).__name__}: {ex}")

    def add_source_evidence_nodes(self) -> None:
        span_ids = sorted({e.evidence_span_id for e in self.edges.values() if e.evidence_span_id})
        for sid in span_ids:
            r = self.conn.execute("SELECT s.span_id,s.source_id,s.span_type,substr(s.normalised_text,1,120) txt,d.title FROM source_span s LEFT JOIN source_document d ON d.source_id=s.source_id WHERE s.span_id=?", (sid,)).fetchone()
            if not r: continue
            self.add_node(sid, "SourceSpan", f"{r['span_type']} from {r['title'] or r['source_id']}: {r['txt'] or ''}", source_table="source_span", source_pk=sid)
            self.add_node(f"source_document:{r['source_id']}", "SourceDocument", r["title"] or r["source_id"], source_table="source_document", source_pk=r["source_id"])
            self.add_edge(f"source_document:{r['source_id']}", "CONTAINS", sid, sid, 1.0, "deterministic", "accepted_candidate", "Source document contains the cited evidence span.")

    def write_csvs_and_load(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        self.add_source_evidence_nodes()
        node_fields = ["node_id","node_type","label","source_table","source_pk","properties_json","effective_from","effective_to","review_status"]
        edge_fields = ["source_node_id","edge_type","target_node_id","evidence_span_id","confidence","extraction_method","review_status","explanation"]
        with (OUT/"graph_nodes_candidate.csv").open("w", newline="", encoding="utf-8") as f:
            w=csv.DictWriter(f, fieldnames=node_fields); w.writeheader(); w.writerows(asdict(n) for n in self.nodes.values())
        with (OUT/"graph_edges_candidate.csv").open("w", newline="", encoding="utf-8") as f:
            w=csv.DictWriter(f, fieldnames=edge_fields); w.writeheader(); w.writerows(asdict(e) for e in self.edges.values())
        with (OUT/"low_confidence_edges.csv").open("w", newline="", encoding="utf-8") as f:
            w=csv.DictWriter(f, fieldnames=edge_fields); w.writeheader(); w.writerows(asdict(e) for e in self.edges.values() if e.confidence < 0.75)
        with (OUT/"missing_evidence_edges.csv").open("w", newline="", encoding="utf-8") as f:
            w=csv.DictWriter(f, fieldnames=edge_fields); w.writeheader(); w.writerows(asdict(e) for e in self.missing)
        self.conn.execute("DELETE FROM graph_edge")
        self.conn.execute("DELETE FROM graph_node")
        for n in self.nodes.values():
            # Blank source_table/source_pk must be SQL NULL. The schema has a unique
            # projection index for non-null source_table/source_pk; empty strings would
            # otherwise collide and overwrite unrelated semantic candidate nodes.
            self.conn.execute("INSERT OR REPLACE INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json,effective_from,effective_to,review_status) VALUES (?,?,?,?,?,?,?,?,?)",
                              (n.node_id,n.node_type,n.label,n.source_table or None,n.source_pk or None,n.properties_json,n.effective_from or None,n.effective_to or None,n.review_status))
        for e in self.edges.values():
            eid = "edge:" + h(e.source_node_id,e.edge_type,e.target_node_id,e.evidence_span_id,e.extraction_method)
            self.conn.execute("INSERT OR REPLACE INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,properties_json,evidence_span_id,confidence,extraction_method,review_status) VALUES (?,?,?,?,?,?,?,?,?)",
                              (eid,e.source_node_id,e.target_node_id,e.edge_type,json.dumps({"explanation":e.explanation}),e.evidence_span_id,e.confidence,e.extraction_method,e.review_status))
        self.conn.commit()
        report = [
            "# COR011 semantic extraction report", "", f"Generated: {datetime.now(timezone.utc).isoformat()}", "",
            f"Low-cost LLM model: `{MODEL}`", f"Prompt version: `{PROMPT_VERSION}`", "",
            "## Counts", "", f"- candidate nodes: {len(self.nodes)}", f"- candidate edges: {len(self.edges)}", f"- accepted_candidate edges: {sum(1 for e in self.edges.values() if e.review_status=='accepted_candidate')}", f"- candidate review edges: {sum(1 for e in self.edges.values() if e.review_status=='candidate')}", f"- low-confidence edges: {sum(1 for e in self.edges.values() if e.confidence < 0.75)}", f"- missing-evidence attempted edges: {len(self.missing)}", f"- LLM calls: {self.llm_calls}", f"- LLM accepted/proposed edges loaded: {self.llm_edges}", f"- LLM errors: {len(self.llm_errors)}", "",
            "## Evidence policy", "Every loaded candidate edge has a non-empty `evidence_span_id`. Missing-evidence attempted edges are written separately and not loaded.", "",
            "## Outputs", "", f"- graph_nodes_candidate.csv: `{OUT/'graph_nodes_candidate.csv'}`", f"- graph_edges_candidate.csv: `{OUT/'graph_edges_candidate.csv'}`", f"- low_confidence_edges.csv: `{OUT/'low_confidence_edges.csv'}`", f"- missing_evidence_edges.csv: `{OUT/'missing_evidence_edges.csv'}`", "",
            "## LLM errors", "",
        ]
        report += [f"- {x}" for x in self.llm_errors] or ["- none"]
        (OUT/"semantic_extraction_report.md").write_text("\n".join(report), encoding="utf-8")

    def run(self) -> None:
        self.seed_nodes()
        self.add_template_nodes_edges()
        self.add_legal_concept_edges()
        self.add_validation_edges()
        self.run_llm()
        self.write_csvs_and_load()
        print(json.dumps({"nodes":len(self.nodes),"edges":len(self.edges),"missing":len(self.missing),"low":sum(1 for e in self.edges.values() if e.confidence < 0.75),"llm_calls":self.llm_calls,"llm_edges":self.llm_edges,"llm_errors":self.llm_errors}, indent=2))

if __name__ == "__main__":
    Extractor().run()
