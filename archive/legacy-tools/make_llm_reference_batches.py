#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sqlite3, hashlib, re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DB=ROOT/'backend/data/rulebook.sqlite3'
OUT=ROOT/'logs/llm-reference-batches'
NODE_TYPES=('rule','chapter','part','guidance_document','guidance_section','guidance_paragraph','defined_term','legal_instrument')

def sha1(s): return hashlib.sha1(s.encode()).hexdigest()
def ctx(meta): return {k:meta.get(k) for k in ('part_title','chapter_title','document_title','source','rule_number','paragraph_number','section_number') if meta.get(k)}
def main():
 p=argparse.ArgumentParser(); p.add_argument('--batch-size',type=int,default=50); p.add_argument('--max-chars',type=int,default=6000); p.add_argument('--limit',type=int); p.add_argument('--start-index',type=int,default=1); p.add_argument('--only-unbatched',action='store_true'); args=p.parse_args()
 OUT.mkdir(parents=True,exist_ok=True)
 con=sqlite3.connect(DB); con.row_factory=sqlite3.Row
 done=set()
 for f in OUT.glob('batch-*.extracted.jsonl'):
  for line in f.read_text().splitlines():
   try: done.add(json.loads(line)['node_id'])
   except Exception: pass
 rows=[]
 q=f"""
 SELECT id,node_type,title,text,url,metadata_json
 FROM node
 WHERE node_type IN ({','.join('?' for _ in NODE_TYPES)})
   AND (COALESCE(text,'')<>'' OR COALESCE(title,'')<>'')
 ORDER BY CASE node_type WHEN 'rule' THEN 1 WHEN 'guidance_paragraph' THEN 2 WHEN 'defined_term' THEN 3 ELSE 4 END, title,id
 """
 for r in con.execute(q,NODE_TYPES):
  if args.only_unbatched and r['id'] in done: continue
  meta=json.loads(r['metadata_json'] or '{}')
  text=(r['text'] or r['title'] or '')[:args.max_chars]
  rows.append({'id':r['id'],'node_type':r['node_type'],'title':r['title'],'url':r['url'],'context':ctx(meta),'text':text})
  if args.limit and len(rows)>=args.limit: break
 n=0
 for i in range(0,len(rows),args.batch_size):
  idx=args.start_index+n
  path=OUT/f'batch-{idx:06d}.json'
  path.write_text(json.dumps(rows[i:i+args.batch_size],indent=2,ensure_ascii=False))
  n+=1
 print(json.dumps({'nodes':len(rows),'batches':n,'batch_size':args.batch_size,'start_index':args.start_index,'out':str(OUT)},indent=2))
if __name__=='__main__': main()
