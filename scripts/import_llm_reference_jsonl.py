#!/usr/bin/env python3
from __future__ import annotations
import argparse, glob, hashlib, json, sqlite3
from pathlib import Path
import importlib.util
ROOT=Path(__file__).resolve().parents[1]
DB=ROOT/'backend/data/rulebook.sqlite3'
PASS=ROOT/'scripts/llm_reference_pass.py'
spec=importlib.util.spec_from_file_location('llm_reference_pass', PASS)
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

def node_context(row): return mod.node_context(row)
def node_payload(row, max_chars): return mod.node_payload(row, max_chars)
def sha1(s): return hashlib.sha1(s.encode()).hexdigest()
def main():
 p=argparse.ArgumentParser(); p.add_argument('files', nargs='+'); p.add_argument('--db',type=Path,default=DB); p.add_argument('--model',default='openai-codex/gpt-5.5'); p.add_argument('--max-chars',type=int,default=6000); args=p.parse_args()
 conn=mod.connect(args.db)
 imported=refs=0
 for pat in args.files:
  for path in sorted(glob.glob(pat)):
   for line in Path(path).read_text().splitlines():
    obj=json.loads(line); node_id=obj['node_id']; data={'references': obj.get('references') or []}
    r=conn.execute('select id,node_type,title,text,url,metadata_json from node where id=?',(node_id,)).fetchone()
    if not r: raise SystemExit(f'missing node {node_id}')
    text=node_payload(r,args.max_chars)
    h=sha1('\n'.join([r['node_type'] or '', r['title'] or '', node_context(r), text]))
    conn.execute('''
      INSERT INTO llm_reference_extraction (node_id,model,prompt_version,text_hash,status,response_json,error,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
      ON CONFLICT(node_id) DO UPDATE SET model=excluded.model,prompt_version=excluded.prompt_version,text_hash=excluded.text_hash,status='ok',response_json=excluded.response_json,error='',updated_at=CURRENT_TIMESTAMP
    ''',(node_id,args.model,mod.PROMPT_VERSION,h,'ok',json.dumps(data,ensure_ascii=False),''))
    imported+=1; refs+=len(data['references'])
 conn.commit()
 print(json.dumps({'imported_nodes':imported,'references':refs},indent=2))
if __name__=='__main__': main()
