#!/usr/bin/env python3
from __future__ import annotations
import csv, json, re, sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DB = Path('backend/data/rulebook.sqlite3')
OUT = Path('outputs/quantitative-thresholds/quantitative-threshold-report-full.csv')
REVIEW_OUT = Path('outputs/quantitative-thresholds/quantitative-threshold-report.csv')

CURRENCY = r"(?:£\s?\d[\d,]*(?:\.\d+)?\s?(?:m|mn|million|bn|billion|k|thousand)?|\bGBP\s?\d[\d,]*(?:\.\d+)?\s?(?:m|mn|million|bn|billion|k|thousand)?|\b\d[\d,]*(?:\.\d+)?\s?(?:pounds?|sterling)\b)"
PERCENT = r"(?:\b\d+(?:\.\d+)?\s?(?:%|per cent|percent|percentage points?)\b)"
BPS = r"(?:\b\d+(?:\.\d+)?\s?(?:basis points?|bps)\b)"
RATIO = r"(?:\b\d+(?:\.\d+)?\s?times\b|\b\d+(?:\.\d+)?x\b)"
DAYS = r"(?:\b\d+(?:\.\d+)?\s?(?:business\s+)?(?:days?|weeks?|months?|years?|hours?)\b)"
GEN_NUM = r"(?:\b\d[\d,]*(?:\.\d+)?\b)"
COMP = r"(?:at least|at most|more than|less than|greater than|fewer than|no more than|no less than|not less than|not more than|exceeds?|exceeding|below|above|under|over|minimum|max(?:imum)?|threshold|limit|floor|cap|within|by no later than|not exceeding|equal to or greater than|equal to or less than|≥|≤|>=|<=|>|<)"
QUANT_RE = re.compile("|".join([CURRENCY, PERCENT, BPS, RATIO, DAYS]), re.I)
COMP_NUM_RE = re.compile(rf"\b{COMP}\b[^.;:()\n]{{0,90}}{GEN_NUM}|{GEN_NUM}[^.;:()\n]{{0,90}}\b{COMP}\b", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9£])|\n+")

TYPE_PATTERNS = [
    ('currency', re.compile(CURRENCY, re.I)),
    ('percentage', re.compile(PERCENT, re.I)),
    ('basis_points', re.compile(BPS, re.I)),
    ('ratio_multiple', re.compile(RATIO, re.I)),
    ('time_period', re.compile(DAYS, re.I)),
    ('comparative_numeric', COMP_NUM_RE),
]

@dataclass
class Source:
    estate: str
    source_table: str
    source_id: str
    title: str
    url: str
    text: str
    metadata: dict


def meta(row, key='metadata_json'):
    try: return json.loads(row[key] or '{}')
    except Exception: return {}


def classify_guidance(md: dict, title: str) -> str:
    dt=(md.get('document_type') or '').lower()
    t=title.lower()
    if dt == 'statement_of_policy' or '/statements-of-policy/' in (md.get('url') or '') or re.search(r'\bsop\d*[/ -]', t): return 'sop'
    if dt == 'supervisory_statement' or re.search(r'\b(?:ss|lss)\d+[/ -]', t): return 'ss'
    return 'guidance'


def span_locator(md: dict) -> str:
    span_type = md.get('span_type') or ''
    bits = [span_type] if span_type else []
    if md.get('page_number') not in (None, ''):
        bits.append(f"page {md['page_number']}")
    if md.get('sheet_name'):
        bits.append(f"sheet {md['sheet_name']}")
    if md.get('row_number') not in (None, ''):
        bits.append(f"row {md['row_number']}")
    return ' | '.join(bits)


def document_or_part(src: Source, md: dict) -> str:
    # For reporting source spans the document title lives on source_document,
    # while heading_path is usually empty for PDFs and spreadsheets. Put the
    # actual document title in this column so review CSVs group coherently.
    if src.source_table == 'source_span':
        return src.title
    val = md.get('document_title') or md.get('part_title') or md.get('heading_path') or ''
    if val == 'PDF extracted text' and md.get('document_title'):
        return md.get('document_title') or ''
    return val


def paragraph_or_rule(src: Source, md: dict) -> str:
    if src.source_table == 'source_span':
        return span_locator(md)
    return md.get('paragraph_number') or md.get('rule_number') or md.get('display_number') or md.get('span_type') or ''


def sources(conn: sqlite3.Connection) -> Iterable[Source]:
    # Rulebook provisions from graph node table.
    for r in conn.execute("SELECT id,node_type,title,text,url,metadata_json FROM node WHERE node_type='rule' AND COALESCE(text,'')<>''"):
        md=meta(r)
        title=f"{md.get('part_title','')} {r['title']}".strip() or r['title']
        yield Source('rule','node',r['id'],title,r['url'] or '',r['text'] or '',md)
    # Supervisory statements and statements of policy. Prefer paragraph/document text, not empty sections.
    for r in conn.execute("SELECT id,node_type,title,text,url,metadata_json FROM node WHERE node_type IN ('guidance_paragraph','guidance_document') AND COALESCE(text,'')<>''"):
        md=meta(r); estate=classify_guidance({**md,'url':r['url']}, r['title'] or '')
        if estate in {'ss','sop'}:
            yield Source(estate,'node',r['id'],r['title'] or '',r['url'] or '',r['text'] or '',md)
    # Reporting source spans and extracted artefacts.
    for r in conn.execute("""
        SELECT s.span_id id, 'source_span' source_table, COALESCE(d.title,'') title, d.url url,
               COALESCE(s.normalised_text,s.raw_text,'') text,
               json_object('span_type',s.span_type,'page_number',s.page_number,'sheet_name',s.sheet_name,'row_number',s.row_number,'heading_path',s.heading_path) metadata_json
        FROM source_span s JOIN source_document d ON d.source_id=s.source_id
        WHERE COALESCE(s.normalised_text,s.raw_text,'')<>''
    """):
        yield Source('reporting','source_span',r['id'],r['title'] or '',r['url'] or '',r['text'] or '',json.loads(r['metadata_json']))
    for table, idcol, textcols in [
        ('instruction','instruction_id',['instruction_set','text']),
        ('validation_rule','validation_id',['label','expression_text']),
        ('calculation_rule','calculation_id',['label','expression_text']),
        ('template_row','row_id',['row_code','label']),
        ('template_column','column_id',['column_code','label']),
        ('datapoint','datapoint_id',['concept_label','data_type','unit_type']),
        ('reporting_obligation','obligation_id',['data_item_code','title','domain','frequency']),
    ]:
        cols=', '.join([idcol]+textcols)
        for r in conn.execute(f"SELECT {cols} FROM {table}"):
            text=' | '.join(str(r[c] or '') for c in textcols).strip()
            if text:
                yield Source('reporting',table,r[idcol],text[:160],'',text,{})


def snippets(text: str) -> Iterable[str]:
    text=re.sub(r'\s+', ' ', text or '').strip()
    if not text: return
    # sentence-ish split, with a fallback sliding window for table-like text
    parts=[p.strip() for p in SENT_SPLIT.split(text) if p.strip()]
    if len(parts)==1 and len(text)>600:
        parts=[text[i:i+450] for i in range(0,len(text),350)]
    for p in parts:
        if len(p)>900: p=p[:900]
        if QUANT_RE.search(p) or COMP_NUM_RE.search(p):
            yield p


def match_types(s: str):
    out=[]; vals=[]
    for name,rx in TYPE_PATTERNS:
        ms=[m.group(0).strip() for m in rx.finditer(s)]
        if ms:
            out.append(name); vals.extend(ms[:8])
    return out, vals


def main():
    conn=sqlite3.connect(DB); conn.row_factory=sqlite3.Row
    seen=set(); rows=[]
    for src in sources(conn):
        for snip in snippets(src.text):
            types, vals=match_types(snip)
            if not types: continue
            key=(src.estate,src.source_table,src.source_id,snip)
            if key in seen: continue
            seen.add(key)
            md=src.metadata or {}
            rows.append({
                'estate': src.estate,
                'threshold_types': '; '.join(types),
                'matched_values': '; '.join(dict.fromkeys(vals)),
                'source_table': src.source_table,
                'source_id': src.source_id,
                'title': src.title,
                'document_or_part': document_or_part(src, md),
                'paragraph_or_rule': paragraph_or_rule(src, md),
                'url': src.url,
                'snippet': snip,
            })
    rows.sort(key=lambda r:(r['estate'], r['document_or_part'], r['title'], r['source_id'], r['snippet']))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fields=['estate','threshold_types','matched_values','source_table','source_id','title','document_or_part','paragraph_or_rule','url','snippet']
    with OUT.open('w', newline='', encoding='utf-8-sig') as f:
        w=csv.DictWriter(f, fields); w.writeheader(); w.writerows(rows)

    grouped={}
    def norm_snip(x): return re.sub(r'\s+',' ',x).strip().lower()
    for r in rows:
        key=(r['estate'],r['document_or_part'],r['title'],r['url'],norm_snip(r['snippet']))
        g=grouped.setdefault(key,{**r,'occurrence_count':0,'source_ids':[]})
        g['occurrence_count']+=1
        if r['source_id'] not in g['source_ids'] and len(g['source_ids'])<40: g['source_ids'].append(r['source_id'])
        g['threshold_types']='; '.join(sorted(set(g['threshold_types'].split('; '))|set(r['threshold_types'].split('; '))))
        g['matched_values']='; '.join(dict.fromkeys((g['matched_values']+'; '+r['matched_values']).split('; ')))
    review=list(grouped.values())
    for g in review:
        g['source_ids']='; '.join(g['source_ids'])
    review.sort(key=lambda r:(r['estate'], r['document_or_part'], r['title'], -r['occurrence_count'], r['snippet']))
    review_fields=['estate','threshold_types','matched_values','occurrence_count','source_table','source_id','source_ids','title','document_or_part','paragraph_or_rule','url','snippet']
    with REVIEW_OUT.open('w', newline='', encoding='utf-8-sig') as f:
        w=csv.DictWriter(f, review_fields); w.writeheader(); w.writerows(review)
    from collections import Counter
    print('wrote full', OUT, 'rows', len(rows))
    print('wrote review', REVIEW_OUT, 'rows', len(review))
    print('by estate', dict(Counter(r['estate'] for r in review)))
    print('by type')
    c=Counter(t for r in review for t in r['threshold_types'].split('; '))
    print(dict(c))

if __name__=='__main__': main()
