#!/usr/bin/env python3
from __future__ import annotations
import asyncio,csv,json,re
from pathlib import Path
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/broken-reference-check'
rows=json.loads((OUT/'unresolved-external-link-check.json').read_text())
TARGET={"broken","soft_404","error"}
SOFT=re.compile(r"\b404\b|page not found|could not be found|no longer available|content unavailable",re.I)
async def check(browser,row):
    page=await browser.new_page(viewport={"width":1280,"height":1800})
    status=''; title=''; body=''; err=''; final=row['url']
    try:
        resp=await page.goto(row['url'],wait_until='domcontentloaded',timeout=20000)
        if resp: status=resp.status
        final=page.url; title=await page.title(); body=(await page.locator('body').inner_text(timeout=5000))[:1500]
    except Exception as e: err=type(e).__name__+': '+str(e)
    await page.close()
    hay=title+'\n'+body
    if err: cls='browser_error'; reason=err[:240]
    elif status in {404,410} or SOFT.search(hay): cls='browser_broken'; reason=f'HTTP {status}; {title}'[:240]
    elif status and status>=400: cls='browser_suspect'; reason=f'HTTP {status}; {title}'[:240]
    else: cls='browser_ok'; reason=title[:240]
    return {**row,'browser_classification':cls,'browser_status':status,'browser_reason':reason,'browser_final_url':final,'browser_title':title}
async def main():
    todo=[r for r in rows if r['classification'] in TARGET]
    async with async_playwright() as p:
        browser=await p.firefox.launch(headless=True)
        out=[]
        for r in todo:
            out.append(await check(browser,r))
        await browser.close()
    out.sort(key=lambda r:(r['browser_classification'],str(r['title']).lower()))
    (OUT/'confirmed-broken-browser-check.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
    fields=['browser_classification','browser_status','browser_reason','classification','status','reason','title','url','browser_final_url','live_edges','target_id','stable_key']
    with (OUT/'confirmed-broken-browser-check.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fields); w.writeheader(); [w.writerow({k:r.get(k,'') for k in fields}) for r in out]
    summary={}
    for r in out: summary[r['browser_classification']]=summary.get(r['browser_classification'],0)+1
    print(json.dumps({'checked':len(out),'summary':summary,'csv':str(OUT/'confirmed-broken-browser-check.csv')},indent=2))
if __name__=='__main__': asyncio.run(main())
