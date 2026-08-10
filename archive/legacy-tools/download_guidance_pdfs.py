#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, re, sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

BASE='https://www.prarulebook.co.uk'
UA='rulebookexp-guidance-pdf-downloader/0.1 (+local research prototype)'
DOC_TYPES={'supervisory_statement','statement_of_policy'}
NOT_IN_FORCE_RE=re.compile(r'\b(deleted|no longer in force|not in force|has been deleted|withdrawn)\b',re.I)
PDF_RE=re.compile(r'\.pdf(?:$|[?#])',re.I)
BOE_PUB_RE=re.compile(r'https?://www\.bankofengland\.co\.uk/(?:prudential-regulation/)?publication/',re.I)
DOC_CODE_RE=re.compile(r'\b((?:L?SS|SoP)\s*\d+\s*/\s*\d+|PS\s*\d+\s*/\s*\d+|CP\s*\d+\s*/\s*\d+)\b',re.I)

MANUAL_PDF_OR_PUBLICATION = {
    "SS1/23": "https://www.bankofengland.co.uk/prudential-regulation/publication/2023/may/model-risk-management-principles-for-banks-ss",
    "SS26/15": "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/supervisory-statement/2018/ss2615update-october-18.pdf",
    "SoP1/19": "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/statement-of-policy/2026/sop119-may-2026-update",
}

@dataclass
class Item:
    node_id:str; title:str; document_type:str; rulebook_url:str; status:str; reason:str
    pdf_url:str=''; source_pdf:str=''; bytes:int=0; sha256:str=''; publication_url:str=''; candidates:list[str]|None=None

def norm(u,base=BASE): return urljoin(base,u)
def safe_name(title,url):
    m=re.match(r'\s*((?:L?SS|SoP)\s*\d+/\d+|(?:L?SS|SoP)\d+/\d+)',title,re.I)
    prefix=m.group(1).replace('/','-') if m else Path(urlparse(url).path).stem
    slug=re.sub(r'[^A-Za-z0-9._-]+','-', (prefix+'-'+title)[:145]).strip('-.')
    return (slug or hashlib.sha1(url.encode()).hexdigest()[:12])+'.pdf'
def canonical_code(value):
    m=DOC_CODE_RE.search(value or '')
    return re.sub(r'\s+','',m.group(1)).upper() if m else ''
def code_family(code):
    return re.match(r'[A-Z]+', code or '').group(0) if re.match(r'[A-Z]+', code or '') else ''
def pdf_text_sample(path, max_pages=4, max_chars=8000):
    reader=PdfReader(str(path))
    chunks=[]
    for page in reader.pages[:max_pages]:
        chunks.append(page.extract_text() or '')
        if sum(len(c) for c in chunks) >= max_chars:
            break
    return '\n'.join(chunks)[:max_chars]
def score_pdf_candidate(url, expected_code):
    u=url.lower()
    score=0
    if expected_code and expected_code.lower().replace('/','') in re.sub(r'[^a-z0-9]','',u): score-=25
    if 'supervisory-statement' in u or '/ss' in u: score-=12
    if 'statement-of-policy' in u or '/sop' in u: score-=12
    if '/policy-statement/' in u or '/ps' in u: score+=12
    if '/consultation-paper/' in u or '/cp' in u: score+=15
    if 'appendix' in u or 'app' in u: score+=4
    return score
def validate_pdf(path, pdf_url, title, document_type):
    """Return (ok, reason) after checking the downloaded PDF looks like the requested SS/SoP.

    PRA Rulebook pages often link to a Bank publication page containing multiple PDFs. The
    first PDF may be a policy statement, consultation paper, approach document, or legal
    instrument rather than the live supervisory statement / statement of policy. Accept only
    PDFs whose first pages contain the expected document code, or, for code-less SoPs, whose
    title/family is strongly consistent.
    """
    expected=canonical_code(title)
    sample=pdf_text_sample(path)
    compact_sample=re.sub(r'\s+','',sample).upper()
    url_l=pdf_url.lower()
    codes={re.sub(r'\s+','',m.group(1)).upper() for m in DOC_CODE_RE.finditer(sample)}
    if expected:
        first_code=next((re.sub(r'\s+','',m.group(1)).upper() for m in DOC_CODE_RE.finditer(sample)), '')
        if ('/policy-statement/' in url_l or '/consultation-paper/' in url_l) and code_family(first_code) in {'PS','CP'}:
            return False, f'policy_or_consultation_wrapper expected={expected} first_code={first_code} found={sorted(codes)[:8]}'
        if expected in codes or expected.replace('/','') in re.sub(r'[^A-Z0-9]','',compact_sample):
            return True, 'validated_expected_code'
        exp_family=code_family(expected)
        wrong_families={code_family(c) for c in codes if code_family(c)} - {exp_family}
        if wrong_families or '/policy-statement/' in url_l or '/consultation-paper/' in url_l:
            return False, f'expected_code_absent expected={expected} found={sorted(codes)[:8]}'
        return False, f'expected_code_absent expected={expected} found={sorted(codes)[:8]}'
    # Some statements of policy do not carry a SoP number in their title. Avoid accepting
    # obvious PS/CP/legal-instrument PDFs as stand-ins.
    if document_type == 'statement_of_policy':
        if '/policy-statement/' in url_l or '/consultation-paper/' in url_l or codes & {c for c in codes if code_family(c) in {'PS','CP'}}:
            return False, f'unexpected_policy_or_consultation_pdf found={sorted(codes)[:8]}'
        if 'statement of policy' in sample[:3000].lower() or 'statement-of-policy' in url_l:
            return True, 'validated_statement_of_policy'
    return True, 'validated_no_code_available'
def page_cache_name(url): return hashlib.sha1(url.encode()).hexdigest()+'.html'
def get(session,url,cache_dir=None,refresh=False):
    if cache_dir:
        p=cache_dir/page_cache_name(url)
        if p.exists() and not refresh: return p.read_text(encoding='utf-8')
    r=session.get(url,timeout=60); r.raise_for_status()
    if cache_dir: p.write_text(r.text,encoding='utf-8')
    return r.text
def header_text(soup):
    bits=[]
    # Only inspect page title and top status/notice material. Do not scan the full page:
    # related links and previous-version sections can mention deleted/superseded material.
    for sel in ['.govuk-notification-banner','.notification','.alert','.banner','.notice','main h1','h1']:
        for el in soup.select(sel)[:3]:
            txt=el.get_text(' ',strip=True)
            if txt and txt not in bits: bits.append(txt)
    h1=soup.find('h1')
    if h1:
        parent=h1.parent
        seen=0
        for sib in list(h1.find_next_siblings())[:8]:
            if sib.name in {'nav','ul','ol','footer'}: break
            txt=sib.get_text(' ',strip=True)
            if txt:
                bits.append(txt); seen+=1
            if seen>=3: break
    return '\n'.join(bits)[:2500]
def pdf_links_from_page(soup,base_url):
    out=[]
    for a in soup.find_all('a',href=True):
        href=norm(a['href'],base_url); text=a.get_text(' ',strip=True)
        if '%s' in href: continue
        if PDF_RE.search(href) or '(pdf)' in text.lower() or text.strip().lower()=='pdf':
            if PDF_RE.search(href) and href not in out: out.append(href)
    return out
def publication_links_from_rulebook(soup,base_url):
    out=[]
    for a in soup.find_all('a',href=True):
        href=norm(a['href'],base_url); text=a.get_text(' ',strip=True).lower()
        if BOE_PUB_RE.search(href) and href not in out:
            # Prefer explicit 'available ... here' source links over policy statement related links.
            score=0 if ('available' in text or text=='here' or 'boe website' in text or 'latest version' in text or 'revised version' in text or 'updated' in text) else 1
            out.append((score,href))
    return [h for _,h in sorted(out)]
def load_docs(conn):
    conn.row_factory=sqlite3.Row
    for r in conn.execute("select id,title,url,metadata_json from node where node_type='guidance_document' order by title,url"):
        meta=json.loads(r['metadata_json'] or '{}')
        if meta.get('document_type') in DOC_TYPES: yield r,meta

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db',type=Path,default=Path('backend/data/rulebook.sqlite3'))
    ap.add_argument('--out-dir',type=Path,default=Path('backend/data/raw/guidance-pdfs'))
    ap.add_argument('--refresh',action='store_true')
    ap.add_argument('--limit',type=int)
    args=ap.parse_args()
    args.out_dir.mkdir(parents=True,exist_ok=True); pages=args.out_dir/'pages'; files=args.out_dir/'files'; pages.mkdir(exist_ok=True); files.mkdir(exist_ok=True)
    s=requests.Session(); s.headers.update({'User-Agent':UA})
    conn=sqlite3.connect(args.db); items=[]
    for r,meta in load_docs(conn):
        if args.limit and len(items)>=args.limit: break
        url=r['url']
        try:
            html=get(s,url,pages,args.refresh); soup=BeautifulSoup(html,'html.parser')
            h=header_text(soup); combined=(r['title']+'\n'+h)[:7000]
            if '(Deleted)' in r['title'] or r['title'].lower().startswith('deleted-') or NOT_IN_FORCE_RE.search(combined):
                items.append(Item(r['id'],r['title'],meta.get('document_type',''),url,'skipped','not_in_force_or_deleted_header',candidates=[])); continue
            candidates=pdf_links_from_page(soup,url)
            manual=''
            for code, manual_url in MANUAL_PDF_OR_PUBLICATION.items():
                if code in r['title']:
                    manual=manual_url; break
            pubs=publication_links_from_rulebook(soup,url)
            if manual and not PDF_RE.search(manual) and '/-/media/' not in manual:
                pubs=[manual]+[p for p in pubs if p!=manual]
            pub_url=pubs[0] if pubs else ''
            if manual and (PDF_RE.search(manual) or '/-/media/' in manual):
                candidates=[manual]+[c for c in candidates if c!=manual]
            if pub_url:
                pub_html=get(s,pub_url,pages,args.refresh); pub_soup=BeautifulSoup(pub_html,'html.parser')
                pub_pdfs=pdf_links_from_page(pub_soup,pub_url)
                # BoE publication pages normally list the current/latest PDF first, followed by earlier versions.
                # Preserve that order. Only filter out obvious CP/PS/background PDFs when same-page SS/SoP PDFs exist.
                primary=[u for u in pub_pdfs if 'supervisory-statement' in u.lower() or 'statement-of-policy' in u.lower()]
                if primary:
                    pub_pdfs=primary + [u for u in pub_pdfs if u not in primary]
                candidates=pub_pdfs + [c for c in candidates if c not in pub_pdfs]
            expected=canonical_code(r['title'])
            candidates=sorted(candidates, key=lambda u: score_pdf_candidate(u, expected))
            if not candidates:
                items.append(Item(r['id'],r['title'],meta.get('document_type',''),url,'error','no_pdf_candidate',publication_url=pub_url,candidates=[])); continue
            errs=[]; saved=None
            for pdf in candidates:
                try:
                    rr=s.get(pdf,timeout=90); rr.raise_for_status()
                    ctype=rr.headers.get('content-type','').lower()
                    if not (rr.content.startswith(b'%PDF') or 'pdf' in ctype):
                        raise RuntimeError(f'not_pdf content_type={ctype}')
                    dest=files/safe_name(r['title'],pdf)
                    if not dest.exists() or args.refresh: dest.write_bytes(rr.content)
                    ok, reason=validate_pdf(dest,pdf,r['title'],meta.get('document_type',''))
                    if not ok:
                        raise RuntimeError(reason)
                    sha=hashlib.sha256(dest.read_bytes()).hexdigest()
                    saved=Item(r['id'],r['title'],meta.get('document_type',''),url,'downloaded',reason,pdf,str(dest),dest.stat().st_size,sha,pub_url,candidates[:12])
                    break
                except Exception as exc:
                    errs.append(f'{pdf}: {type(exc).__name__}: {exc}')
            if saved: items.append(saved)
            else: items.append(Item(r['id'],r['title'],meta.get('document_type',''),url,'error','all_pdf_candidates_failed: '+' | '.join(errs[:5]),publication_url=pub_url,candidates=candidates[:12]))
        except Exception as exc:
            items.append(Item(r['id'],r['title'],meta.get('document_type',''),url,'error',f'{type(exc).__name__}: {exc}',candidates=[]))
    manifest=args.out_dir/'manifest.json'; manifest.write_text(json.dumps([asdict(i) for i in items],indent=2,ensure_ascii=False),encoding='utf-8')
    counts={}
    for i in items:
        counts[i.status]=counts.get(i.status,0)+1; counts[f'{i.document_type}:{i.status}']=counts.get(f'{i.document_type}:{i.status}',0)+1
    summary={'counts':counts,'manifest':str(manifest)}
    (args.out_dir/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))
if __name__=='__main__': main()
