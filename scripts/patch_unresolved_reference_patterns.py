#!/usr/bin/env python3
from __future__ import annotations
import json, re, shutil, sqlite3
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

DB=Path('backend/data/rulebook.sqlite3')
PREF=['rule','part','chapter','guidance_paragraph','guidance_section','guidance_document','reporting_obligation','template','data_item','defined_term']
PREF_INDEX={t:i for i,t in enumerate(PREF)}
AUTHORITY_DOMAINS={
 'www.bankofengland.co.uk':'Bank of England', 'bankofengland.co.uk':'Bank of England',
 'www.fca.org.uk':'FCA', 'fca.org.uk':'FCA', 'www.frc.org.uk':'FRC', 'frc.org.uk':'FRC',
 'www.financialstabilityboard.org':'FSB', 'financialstabilityboard.org':'FSB',
 'www.iosco.org':'IOSCO', 'iosco.org':'IOSCO', 'www.iaisweb.org':'IAIS', 'iaisweb.org':'IAIS',
 'www.legislation.gov.uk':'UK legislation', 'legislation.gov.uk':'UK legislation',
 'eur-lex.europa.eu':'EUR-Lex', 'ec.europa.eu':'European Commission', 'www.publications.parliament.uk':'UK Parliament',
}

def clean(v:str)->str:
    if not v: return ''
    return v.removeprefix('url:').removeprefix('external:').replace('http://www.prarulebook.co.uk/','https://www.prarulebook.co.uk/').replace('http://prarulebook.co.uk/','https://www.prarulebook.co.uk/')
def base(v:str)->str: return clean(v).split('#',1)[0].rstrip('/')
def normtitle(s:str)->str: return re.sub(r'\s+',' ',re.sub(r'[–—]','-',s or '').strip().lower())
def slug(v:str)->str:
    b=base(v); m=re.search(r'/(pra-rules|guidance)/(?:[^/]+/)?([^/#?]+)', b); return m.group(2) if m else ''
def host(v:str)->str:
    from urllib.parse import urlparse
    u=clean(v)
    if not re.match(r'https?://',u): return ''
    return urlparse(u).netloc.lower()
def choose(cands,title='',prefer_doc=False):
    def score(n):
        typ=n['node_type']
        s=PREF_INDEX.get(typ,99)
        if prefer_doc and typ in {'guidance_document','part'}: s-=20
        if normtitle(n['title'])==normtitle(title): s-=10
        if typ=='defined_term': s+=20
        return (s,n['stable_key'])
    return sorted(cands,key=score)[0] if cands else None

def main():
    backup=DB.with_suffix(DB.suffix+f'.bak-patterns-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}')
    shutil.copy2(DB,backup)
    conn=sqlite3.connect(DB); conn.row_factory=sqlite3.Row
    placeholders=conn.execute("""SELECT * FROM node WHERE node_type IN ('external_reference','rule_reference') AND json_extract(metadata_json,'$.placeholder')=1 AND id IN (SELECT to_node_id FROM edge WHERE edge_type='references')""").fetchall()
    real=conn.execute("SELECT * FROM node WHERE COALESCE(json_extract(metadata_json,'$.placeholder'),0)!=1").fetchall()
    by_url=defaultdict(list); by_base=defaultdict(list); by_slug=defaultdict(list)
    for n in real:
        for v in {n['url'] or '', n['stable_key'] or ''}:
            if clean(v): by_url[clean(v)].append(n)
            if base(v): by_base[base(v)].append(n)
            if slug(v): by_slug[slug(v)].append(n)
    resolved=Counter(); labelled=Counter(); skipped=Counter()
    for p in placeholders:
        meta=json.loads(p['metadata_json'] or '{}')
        vals={p['url'] or '', p['stable_key'] or ''}
        target=None; basis=''
        # exact URL/key matches are safe, including fragment anchors.
        for v in vals:
            c=by_url.get(clean(v),[])
            if c:
                target=choose(c,p['title']); basis='exact_existing_url'; break
        # dated guidance/doc slug missing date is safe only to doc/part, not arbitrary paragraph.
        if not target:
            for v in vals:
                c=[n for n in by_slug.get(slug(v),[]) if n['node_type'] in {'guidance_document','part'}]
                if c and ('/guidance/' in clean(v) or '/pra-rules/' in clean(v)):
                    target=choose(c,p['title'],prefer_doc=True); basis='dated_slug_existing_document'; break
        # base URL fallback only for prarulebook URLs with a fragment and a unique doc/part.
        if not target:
            for v in vals:
                cv=clean(v)
                c=[n for n in by_base.get(base(v),[]) if n['node_type'] in {'guidance_document','part'}]
                if '#' in cv and len({n['id'] for n in c})==1:
                    target=c[0]; basis='base_existing_document'; break
        if target:
            conn.execute("UPDATE edge SET to_node_id=?, metadata_json=json_set(COALESCE(NULLIF(metadata_json,''),'{}'),'$.resolved_from_placeholder',?,'$.resolution_basis',?) WHERE to_node_id=?",(target['id'],p['id'],basis,p['id']))
            meta.update({'placeholder':False,'resolved_to_node_id':target['id'],'resolution_basis':basis})
            conn.execute("UPDATE node SET metadata_json=? WHERE id=?",(json.dumps(meta,sort_keys=True),p['id']))
            resolved[basis]+=1
            continue
        h=host(p['url'] or p['stable_key'])
        label=AUTHORITY_DOMAINS.get(h)
        if label:
            meta.update({'placeholder':False,'external_reference_label':label,'resolution_basis':'accepted_external_authority_reference'})
            conn.execute("UPDATE node SET title=?, metadata_json=? WHERE id=?",(label if normtitle(p['title']) in {'here','click here'} else p['title'],json.dumps(meta,sort_keys=True),p['id']))
            labelled[label]+=1
        else:
            skipped['remaining']+=1
    conn.commit()
    print('backup',backup)
    print('resolved',dict(resolved))
    print('labelled',dict(labelled))
    print('skipped',dict(skipped))
if __name__=='__main__': main()
