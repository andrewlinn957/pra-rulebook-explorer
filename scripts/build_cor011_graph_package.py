#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
OUT = ROOT / "backend/data/raw/reporting-sources/cor011-lcr-final/graph-package"
SEM = ROOT / "backend/data/raw/reporting-sources/cor011-lcr-final/semantic-extraction"

CORE = "data_item:COR011"
LOW_CONF = 0.75

QUERY_SQL = """-- COR011 graph QA/example queries

-- 1. Show everything directly connected to COR011
SELECT e.edge_type, e.review_status, e.confidence,
       src.node_id AS source_node_id, src.label AS source_label,
       tgt.node_id AS target_node_id, tgt.label AS target_label,
       e.evidence_span_id
FROM graph_edge e
JOIN graph_node src ON src.node_id = e.source_node_id
JOIN graph_node tgt ON tgt.node_id = e.target_node_id
WHERE e.source_node_id = 'data_item:COR011' OR e.target_node_id = 'data_item:COR011'
ORDER BY e.edge_type, tgt.label;

-- 2. Show all rulebook provisions that COR011 depends on
WITH RECURSIVE walk(node_id, depth, path) AS (
  VALUES ('data_item:COR011', 0, 'data_item:COR011')
  UNION ALL
  SELECT CASE WHEN e.source_node_id = walk.node_id THEN e.target_node_id ELSE e.source_node_id END,
         depth + 1,
         path || ' -> ' || CASE WHEN e.source_node_id = walk.node_id THEN e.target_node_id ELSE e.source_node_id END
  FROM walk
  JOIN graph_edge e ON e.source_node_id = walk.node_id OR e.target_node_id = walk.node_id
  WHERE depth < 4
    AND e.edge_type IN ('LEGAL_BASIS','ESTABLISHED_BY','REFERENCES_RULE','HAS_SCOPE_RULE','SUBJECT_TO','APPLIES_TO')
    AND instr(path, CASE WHEN e.source_node_id = walk.node_id THEN e.target_node_id ELSE e.source_node_id END) = 0
)
SELECT DISTINCT n.node_id, n.label, w.depth, w.path
FROM walk w JOIN graph_node n ON n.node_id = w.node_id
WHERE n.node_type = 'Provision'
ORDER BY w.depth, n.label;

-- 3. Show all templates and datapoints affected by Article 34
WITH affected AS (
  SELECT e.source_node_id AS node_id FROM graph_edge e WHERE e.target_node_id IN ('permission:LCR.Article34','provision:LCR.Article34')
  UNION SELECT e.target_node_id FROM graph_edge e WHERE e.source_node_id IN ('permission:LCR.Article34','provision:LCR.Article34')
  UNION SELECT e.source_node_id FROM graph_edge e WHERE e.target_node_id='concept:Inflows'
  UNION SELECT e.source_node_id FROM graph_edge e WHERE e.target_node_id='template:C74.00'
)
SELECT DISTINCT n.node_type, n.node_id, n.label
FROM affected a JOIN graph_node n ON n.node_id=a.node_id
WHERE n.node_type IN ('Template','DataPoint','TemplateRow','TemplateColumn')
ORDER BY n.node_type, n.label;

-- 4. Show all COR011 datapoints that feed net liquidity outflows
SELECT DISTINCT dp.node_id, dp.label, e.edge_type, e.confidence, e.evidence_span_id
FROM graph_node dp
JOIN graph_edge e ON e.source_node_id = dp.node_id
WHERE dp.node_type='DataPoint'
  AND e.edge_type='REPORTS_CONCEPT'
  AND e.target_node_id IN ('concept:NetLiquidityOutflows','concept:Outflows','concept:Inflows')
ORDER BY dp.node_id;

-- 5. Show all permissions that may affect the reported LCR
SELECT p.node_id, p.label, e.edge_type, e.confidence, e.evidence_span_id
FROM graph_edge e
JOIN graph_node p ON p.node_id = e.target_node_id
WHERE e.source_node_id IN ('data_item:COR011','reporting_obligation:COR011')
  AND e.edge_type='MAY_BE_AFFECTED_BY_PERMISSION'
  AND p.node_type='Permission'
ORDER BY p.label;

-- 6. Show the path from C74.00 to LCR Article 34
WITH RECURSIVE walk(node_id, depth, path) AS (
  VALUES ('template:C74.00', 0, 'template:C74.00')
  UNION ALL
  SELECT CASE WHEN e.source_node_id = walk.node_id THEN e.target_node_id ELSE e.source_node_id END,
         depth + 1,
         path || ' -> ' || CASE WHEN e.source_node_id = walk.node_id THEN e.target_node_id ELSE e.source_node_id END
  FROM walk JOIN graph_edge e ON e.source_node_id=walk.node_id OR e.target_node_id=walk.node_id
  WHERE depth < 6
    AND instr(path, CASE WHEN e.source_node_id = walk.node_id THEN e.target_node_id ELSE e.source_node_id END)=0
)
SELECT path FROM walk WHERE node_id IN ('provision:LCR.Article34','permission:LCR.Article34') ORDER BY depth LIMIT 5;

-- 7. Show what would be affected if Article 33 changed
WITH RECURSIVE affected(node_id, depth, path) AS (
  VALUES ('provision:LCR.Article33', 0, 'provision:LCR.Article33')
  UNION ALL
  SELECT CASE WHEN e.source_node_id = affected.node_id THEN e.target_node_id ELSE e.source_node_id END,
         depth + 1,
         path || ' -> ' || CASE WHEN e.source_node_id = affected.node_id THEN e.target_node_id ELSE e.source_node_id END
  FROM affected JOIN graph_edge e ON e.source_node_id=affected.node_id OR e.target_node_id=affected.node_id
  WHERE depth < 4
    AND e.edge_type IN ('LEGAL_BASIS','MAY_BE_AFFECTED_BY_PERMISSION','REPORTS_CONCEPT','USES_INPUT','FEEDS_CALCULATION','USES_TEMPLATE','HAS_DATAPOINT')
    AND instr(path, CASE WHEN e.source_node_id = affected.node_id THEN e.target_node_id ELSE e.source_node_id END)=0
)
SELECT DISTINCT n.node_type, n.node_id, n.label, a.depth, a.path
FROM affected a JOIN graph_node n ON n.node_id=a.node_id
WHERE a.depth > 0
ORDER BY a.depth, n.node_type, n.label;
"""


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]

def one(conn, sql, params=()):
    r=conn.execute(sql, params).fetchone(); return dict(r) if r else None

def evidence(conn, span_id):
    if not span_id: return None
    return one(conn, """
      SELECT s.span_id,s.source_id,s.span_type,s.page_number,s.sheet_name,s.row_number,s.column_number,s.heading_path,s.anchor,
             s.normalised_text AS text,d.title AS source_title,d.url AS source_url,d.local_path,d.file_type
      FROM source_span s LEFT JOIN source_document d ON d.source_id=s.source_id WHERE s.span_id=?
    """, (span_id,))

def edge_dict(conn, e):
    d=dict(e); d["evidence"]=evidence(conn, d.get("evidence_span_id"));
    try: d["properties"]=json.loads(d.pop("properties_json") or "{}")
    except Exception: d["properties"]={}
    return d

def node_dict(n):
    d=dict(n)
    try: d["properties"]=json.loads(d.pop("properties_json") or "{}")
    except Exception: d["properties"]={}
    return d

def connected_subgraph(conn, seed=CORE, depth=2, limit=1800):
    seen={seed}; q=deque([(seed,0)]); edge_ids=[]
    while q and len(seen)<limit:
        nid,d=q.popleft()
        if d>=depth: continue
        for e in conn.execute("SELECT * FROM graph_edge WHERE source_node_id=? OR target_node_id=? ORDER BY confidence DESC LIMIT 500", (nid,nid)):
            eid=e["edge_id"]
            edge_ids.append(eid)
            other=e["target_node_id"] if e["source_node_id"]==nid else e["source_node_id"]
            if other not in seen:
                seen.add(other); q.append((other,d+1))
    node_list=[node_dict(r) for r in conn.execute(f"SELECT * FROM graph_node WHERE node_id IN ({','.join('?' for _ in seen)})", tuple(seen))]
    uniq_edges=[]; used=set()
    for eid in edge_ids:
        if eid in used: continue
        used.add(eid)
        e=one(conn,"SELECT * FROM graph_edge WHERE edge_id=?",(eid,))
        if e and e["source_node_id"] in seen and e["target_node_id"] in seen: uniq_edges.append(edge_dict(conn,e))
    return {"nodes": node_list, "edges": uniq_edges}

def directly(conn, nid):
    return [edge_dict(conn,e) for e in conn.execute("SELECT * FROM graph_edge WHERE source_node_id=? OR target_node_id=? ORDER BY confidence DESC, edge_type", (nid,nid))]

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob('qa_*.csv'):
        old.unlink()
    conn=sqlite3.connect(DB); conn.row_factory=sqlite3.Row
    # Load accepted + candidate CSVs into graph tables again, preserving statuses.
    nodes=list(csv.DictReader((SEM/'graph_nodes_candidate.csv').open()))
    edges=list(csv.DictReader((SEM/'graph_edges_candidate.csv').open()))
    conn.execute('DELETE FROM graph_edge'); conn.execute('DELETE FROM graph_node')
    for n in nodes:
        conn.execute("INSERT OR REPLACE INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json,effective_from,effective_to,review_status) VALUES (?,?,?,?,?,?,?,?,?)",
                     (n['node_id'],n['node_type'],n['label'],n.get('source_table') or None,n.get('source_pk') or None,n.get('properties_json') or '{}',n.get('effective_from') or None,n.get('effective_to') or None,n.get('review_status') or 'candidate'))
    for e in edges:
        eid='edge:'+hashlib.sha1('|'.join([e['source_node_id'],e['edge_type'],e['target_node_id'],e['evidence_span_id'],e['extraction_method']]).encode()).hexdigest()[:16]
        conn.execute("INSERT OR REPLACE INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,properties_json,evidence_span_id,confidence,extraction_method,review_status) VALUES (?,?,?,?,?,?,?,?,?)",
                     (eid,e['source_node_id'],e['target_node_id'],e['edge_type'],json.dumps({'explanation':e.get('explanation','')}),e['evidence_span_id'],float(e['confidence'] or 0),e['extraction_method'],e.get('review_status') or 'candidate'))
    conn.commit()
    # CSV exports from DB, not stale source.
    with (OUT/'graph_nodes.csv').open('w', newline='', encoding='utf-8') as f:
        data=rows(conn,'SELECT * FROM graph_node ORDER BY node_type,label,node_id')
        w=csv.DictWriter(f, fieldnames=data[0].keys()); w.writeheader(); w.writerows(data)
    with (OUT/'graph_edges.csv').open('w', newline='', encoding='utf-8') as f:
        data=rows(conn,'SELECT * FROM graph_edge ORDER BY edge_type,source_node_id,target_node_id')
        w=csv.DictWriter(f, fieldnames=data[0].keys()); w.writeheader(); w.writerows(data)
    # Package sections.
    sub=connected_subgraph(conn)
    templates=rows(conn,"SELECT * FROM template ORDER BY template_code")
    for t in templates:
        t['rows_count']=conn.execute('SELECT count(*) FROM template_row WHERE template_id=?',(t['template_id'],)).fetchone()[0]
        t['columns_count']=conn.execute('SELECT count(*) FROM template_column WHERE template_id=?',(t['template_id'],)).fetchone()[0]
        t['datapoints_count']=conn.execute('SELECT count(*) FROM datapoint WHERE template_id=?',(t['template_id'],)).fetchone()[0]
        t['sample_rows']=rows(conn,'SELECT row_id,row_code,label FROM template_row WHERE template_id=? ORDER BY row_order LIMIT 20',(t['template_id'],))
        t['sample_columns']=rows(conn,'SELECT column_id,column_code,label,unit_type FROM template_column WHERE template_id=? ORDER BY column_order LIMIT 20',(t['template_id'],))
    package={
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'core_node_id': CORE,
        'summary': {
            'nodes': conn.execute('SELECT count(*) FROM graph_node').fetchone()[0],
            'edges': conn.execute('SELECT count(*) FROM graph_edge').fetchone()[0],
            'accepted_candidate_edges': conn.execute("SELECT count(*) FROM graph_edge WHERE review_status='accepted_candidate'").fetchone()[0],
            'candidate_edges': conn.execute("SELECT count(*) FROM graph_edge WHERE review_status='candidate'").fetchone()[0],
            'low_confidence_edges': conn.execute('SELECT count(*) FROM graph_edge WHERE confidence < ?', (LOW_CONF,)).fetchone()[0],
        },
        'legal_basis': {
            'direct_edges': directly(conn,'reporting_obligation:COR011') + directly(conn,CORE),
            'provisions': rows(conn,"SELECT * FROM graph_node WHERE node_type='Provision' ORDER BY label"),
            'scope_rules': rows(conn,"SELECT * FROM graph_node WHERE node_type='ScopeRule' ORDER BY label"),
        },
        'reporting_artefacts': {
            'template_set': one(conn,"SELECT * FROM graph_node WHERE node_id='template_set:AnnexXXIV'"),
            'instruction_set': one(conn,"SELECT * FROM graph_node WHERE node_id='instruction_set:AnnexXXV'"),
            'templates': templates,
        },
        'prudential_calculation': {
            'metric': one(conn,"SELECT * FROM graph_node WHERE node_id='metric:LiquidityCoverageRatio'"),
            'calculation': one(conn,"SELECT * FROM graph_node WHERE node_id='calculation:LCRFormula'"),
            'dependencies': [edge_dict(conn,e) for e in conn.execute("SELECT * FROM graph_edge WHERE edge_type IN ('CALCULATES','USES_INPUT','FEEDS_CALCULATION','REPORTS_CONCEPT','REPORTS_METRIC') ORDER BY edge_type,confidence DESC LIMIT 500")],
        },
        'permission_hooks': {
            'permissions': rows(conn,"SELECT * FROM graph_node WHERE node_type='Permission' ORDER BY label"),
            'edges': [edge_dict(conn,e) for e in conn.execute("SELECT * FROM graph_edge WHERE source_node_id LIKE 'permission:%' OR target_node_id LIKE 'permission:%' ORDER BY confidence DESC")],
        },
        'evidence': {
            'sample_edges': [edge_dict(conn,e) for e in conn.execute("SELECT * FROM graph_edge ORDER BY confidence DESC LIMIT 120")],
        },
        'graph': sub,
    }
    (OUT/'cor011_package.json').write_text(json.dumps(package, indent=2, ensure_ascii=False), encoding='utf-8')
    (OUT/'example_queries.sql').write_text(QUERY_SQL, encoding='utf-8')
    # QA reports.
    qa=[]
    def add(title, data):
        qa.append((title, data))
    add('reporting obligations with no legal basis', rows(conn,"""
      SELECT n.node_id,n.label FROM graph_node n
      WHERE n.node_type='ReportingObligation' AND NOT EXISTS (
        SELECT 1 FROM graph_edge e WHERE e.source_node_id=n.node_id AND e.edge_type IN ('LEGAL_BASIS','ESTABLISHED_BY'))"""))
    add('templates with no instruction source', rows(conn,"""
      SELECT n.node_id,n.label FROM graph_node n WHERE n.node_type='Template' AND NOT EXISTS (
        SELECT 1 FROM graph_edge e WHERE (e.source_node_id=n.node_id OR e.target_node_id=n.node_id) AND e.edge_type='USES_INSTRUCTIONS')"""))
    add('datapoints with no concept', rows(conn,"""
      SELECT n.node_id,n.label FROM graph_node n WHERE n.node_type='DataPoint' AND NOT EXISTS (
        SELECT 1 FROM graph_edge e WHERE e.source_node_id=n.node_id AND e.edge_type IN ('REPORTS_CONCEPT','REPORTS_METRIC')) LIMIT 1000"""))
    add('concepts with no rule hook', rows(conn,"""
      SELECT n.node_id,n.label FROM graph_node n WHERE n.node_type IN ('Concept','Metric') AND NOT EXISTS (
        SELECT 1 FROM graph_edge e WHERE (e.source_node_id=n.node_id OR e.target_node_id=n.node_id) AND e.edge_type IN ('DEFINES','REFERENCES_RULE','LEGAL_BASIS','CALCULATES'))"""))
    add('permissions with no affected datapoints', rows(conn,"""
      SELECT n.node_id,n.label FROM graph_node n WHERE n.node_type='Permission' AND NOT EXISTS (
        SELECT 1 FROM graph_edge e JOIN graph_node d ON d.node_id IN (e.source_node_id,e.target_node_id)
        WHERE (e.source_node_id=n.node_id OR e.target_node_id=n.node_id) AND d.node_type='DataPoint')"""))
    add('calculation rules with missing inputs', rows(conn,"""
      SELECT n.node_id,n.label FROM graph_node n WHERE n.node_type='CalculationRule' AND NOT EXISTS (
        SELECT 1 FROM graph_edge e WHERE e.source_node_id=n.node_id AND e.edge_type='USES_INPUT')"""))
    add('graph edges with no evidence_span_id', rows(conn,"SELECT edge_id,source_node_id,edge_type,target_node_id FROM graph_edge WHERE coalesce(evidence_span_id,'')=''"))
    add('duplicate nodes', rows(conn,"SELECT node_type,label,count(*) AS n FROM graph_node GROUP BY node_type,label HAVING count(*)>1 ORDER BY n DESC,label LIMIT 1000"))
    unresolved_file = ROOT/'backend/data/raw/reporting-sources/cor011-lcr-final/parsed-load/unresolved_references.csv'
    unresolved=list(csv.DictReader(unresolved_file.open())) if unresolved_file.exists() else []
    low=rows(conn,"SELECT edge_id,source_node_id,edge_type,target_node_id,evidence_span_id,confidence,review_status FROM graph_edge WHERE confidence < ? ORDER BY confidence",(LOW_CONF,))
    add('candidate edges below confidence 0.75', low)
    # Write CSVs for key QA sets too.
    for title,data in qa:
        safe=''.join(ch if ch.isalnum() else '_' for ch in title.lower()).strip('_')
        if data:
            with (OUT/f'qa_{safe}.csv').open('w', newline='', encoding='utf-8') as f:
                w=csv.DictWriter(f, fieldnames=data[0].keys()); w.writeheader(); w.writerows(data)
    md=['# COR011 QA report','',f'Generated: {datetime.now(timezone.utc).isoformat()}','']
    for title,data in qa:
        md += [f'## {title}', '', f'- count: {len(data)}', '']
        for row in data[:20]: md.append(f'- `{row}`')
        if len(data)>20: md.append(f'- … {len(data)-20} more; see CSV in graph-package directory')
        md.append('')
    md += ['## unresolved rule references','',f'- count: {len(unresolved)}','']
    for row in unresolved[:20]: md.append(f'- `{row}`')
    (OUT/'qa_report.md').write_text('\n'.join(md), encoding='utf-8')
    # Build report
    fk=conn.execute('PRAGMA foreign_key_check').fetchall(); integrity=conn.execute('PRAGMA integrity_check').fetchone()[0]
    report=['# COR011 graph build report','',f'Generated: {datetime.now(timezone.utc).isoformat()}','',
            '## Load','',f'- graph_node rows loaded: {package["summary"]["nodes"]}',f'- graph_edge rows loaded: {package["summary"]["edges"]}',
            f'- accepted_candidate edges: {package["summary"]["accepted_candidate_edges"]}',f'- candidate edges: {package["summary"]["candidate_edges"]}',f'- low confidence edges retained as candidate: {package["summary"]["low_confidence_edges"]}',
            '', '## Integrity','',f'- foreign_key_check rows: {len(fk)}',f'- integrity_check: {integrity}',
            '', '## Outputs','',f'- cor011_package.json: `{OUT/'cor011_package.json'}`',f'- graph_nodes.csv: `{OUT/'graph_nodes.csv'}`',f'- graph_edges.csv: `{OUT/'graph_edges.csv'}`',f'- qa_report.md: `{OUT/'qa_report.md'}`',f'- example_queries.sql: `{OUT/'example_queries.sql'}`',
            '', '## Notes','', '- No generic RELATED_TO edges were created.', '- Node and edge types are restricted to the approved vocabulary from the semantic pass.', '- Low-confidence edges are preserved as candidate and listed in QA.']
    (OUT/'graph_build_report.md').write_text('\n'.join(report), encoding='utf-8')
    print(json.dumps({'nodes':package['summary']['nodes'],'edges':package['summary']['edges'],'qa_sections':{t:len(d) for t,d in qa},'unresolved':len(unresolved),'fk_rows':len(fk),'integrity':integrity}, indent=2))

if __name__=='__main__': main()
