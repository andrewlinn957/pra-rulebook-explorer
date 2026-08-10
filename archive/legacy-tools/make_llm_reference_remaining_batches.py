#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DB=ROOT/'backend/data/rulebook.sqlite3'
OUT=ROOT/'logs/llm-reference-batches'
NODE_TYPES=('rule','chapter','part','guidance_document','guidance_section','guidance_paragraph','defined_term')

def ctx(meta):
    return {k:meta.get(k) for k in ('part_title','chapter_title','document_title','source','rule_number','paragraph_number','section_number') if meta.get(k)}

def assigned_ids():
    ids=set()
    for f in OUT.glob('batch-*.json'):
        try:
            for x in json.loads(f.read_text()): ids.add(x['id'])
        except Exception: pass
    return ids

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--batch-size',type=int,default=150)
    p.add_argument('--max-chars',type=int,default=6000)
    p.add_argument('--start-index',type=int,default=12)
    args=p.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    assigned=assigned_ids()
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row
    rows=[]
    q=f"""
    SELECT id,node_type,title,text,url,metadata_json
    FROM node
    WHERE node_type IN ({','.join('?' for _ in NODE_TYPES)})
      AND (COALESCE(text,'')<>'' OR COALESCE(title,'')<>'')
    ORDER BY CASE node_type WHEN 'rule' THEN 1 WHEN 'guidance_paragraph' THEN 2 WHEN 'defined_term' THEN 3 ELSE 4 END, title,id
    """
    for r in con.execute(q,NODE_TYPES):
        if r['id'] in assigned: continue
        meta=json.loads(r['metadata_json'] or '{}')
        rows.append({'id':r['id'],'node_type':r['node_type'],'title':r['title'],'url':r['url'],'context':ctx(meta),'text':(r['text'] or r['title'] or '')[:args.max_chars]})
    n=0
    for i in range(0,len(rows),args.batch_size):
        idx=args.start_index+n
        path=OUT/f'batch-{idx:06d}.json'
        if path.exists(): raise SystemExit(f'would overwrite {path}')
        path.write_text(json.dumps(rows[i:i+args.batch_size],indent=2,ensure_ascii=False))
        n+=1
    print(json.dumps({'existing_assigned':len(assigned),'remaining_nodes':len(rows),'new_batches':n,'batch_size':args.batch_size,'first':args.start_index,'last':args.start_index+n-1},indent=2))
if __name__=='__main__': main()
