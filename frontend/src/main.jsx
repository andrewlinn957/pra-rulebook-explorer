import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import ForceGraph2D from 'react-force-graph-2d';
import { forceCollide } from 'd3-force';
import { isInsuranceNode } from './graphFilters.js';
import { buildUnresolvedActionQueues } from './unresolvedWorkflow.js';
import { displayNodeTitle, documentBadge, relativeNodeRole, edgeDirectionGlyph, edgeDirectionLabel } from './graphPresentation.js';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || '/pra-rulebook-api';
const TYPES = ['contains','references','uses_defined_term','defines','shares_defined_term','has_topic','has_obligation_pattern','has_structured_obligation','shares_obligation_pattern','amends','has_permission'];
const PROVISION_TYPES = ['rule','chapter','guidance_section','guidance_paragraph'];
const NODE_TYPES = [...PROVISION_TYPES,'part','rulebook','defined_term','glossary','crr_terms_list','guidance_document','topic','topic_cluster','obligation_pattern','obligation_statement','legal_instrument','permission','external_reference','rule_reference'];
const DEFAULT_TYPES = new Set(['contains','references','has_topic']);
const REPRESENTATIONS = {
  combined: { label:'Combined', hint:'Legal structure plus rolled-up references, terms, obligations and permissions.', types:[...DEFAULT_TYPES], depth:1, explicitOnly:false },
  hierarchy: { label:'Legal hierarchy', hint:'Parts, articles, chapters, rules and paragraphs only.', types:['contains'], depth:2, explicitOnly:false },
  references: { label:'Cross-references', hint:'Explicit and detected cross-reference/amendment links, with child context so Article-level headings expose paragraph-level references.', types:['contains','references','amends'], depth:2, explicitOnly:false },
  definitions: { label:'Definitions', hint:'Definitions, glossary/CRR term usage, and provisions sharing defined terms.', types:['uses_defined_term','defines','shares_defined_term'], depth:2, explicitOnly:false },
  obligations: { label:'Obligations', hint:'Detected obligation statements, obligation patterns, and provisions with similar obligation patterns.', types:['has_obligation_pattern','has_structured_obligation','shares_obligation_pattern'], depth:1, explicitOnly:false },
};
const EXPLICIT = new Set(['site_structure','html_link','html_anchor_resolved','html_glossary_link','glossary_source','crr_terms_source','legal_instrument_listing','regex_reference','regex_named_reference','llm_extracted_reference','resolved_part_reference','fca_waivers_list']);
const RELATION_LABELS = { contains:'contains / child', references:'Cross-references', uses_defined_term:'Definitions used', defines:'Definitions provided', shares_defined_term:'Shared defined terms', has_topic:'Provisions containing this topic', has_obligation_pattern:'Obligation themes', shares_obligation_pattern:'Similar obligations', has_structured_obligation:'Extracted obligations', amends:'Amendments', has_permission:'Firms with permissions' };
const EVIDENCE_LABELS = { references:'Cross-references', uses_defined_term:'Definitions used by this provision', defines:'Definitions provided here', shares_defined_term:'Provisions sharing defined terms', has_topic:'Provisions containing the same topic', has_obligation_pattern:'Obligation themes found here', shares_obligation_pattern:'Provisions with similar obligations', has_structured_obligation:'Extracted obligation statements', amends:'Legal instruments amending this material', has_permission:'Firms with active permissions' };
const ORIGIN_FILTERS = { all:'All links', explicit:'Direct links', inferred:'Inferred / derived links' };
const EDGE_COLOURS = { contains:'#94a3b8', references:'#2563eb', uses_defined_term:'#d97706', defines:'#ca8a04', shares_defined_term:'#0f766e', has_topic:'#7c3aed', has_obligation_pattern:'#db2777', shares_obligation_pattern:'#ea580c', has_structured_obligation:'#be123c', amends:'#dc2626', has_permission:'#8b5cf6' };
const MATERIAL_COLOURS = { rule:'#2563eb', supervisory_statement:'#16a34a', statement_of_policy:'#0f766e', definition:'#b45309', permission:'#8b5cf6', external_reference:'#64748b', legal_instrument:'#b91c1c', topic:'#7c3aed', obligation_pattern:'#db2777', obligation_statement:'#be123c', analysis:'#9333ea', rulebook:'#6d28d9' };
const CLUSTER_COLOURS = ['#4f7cff','#d28b24','#58a978','#d35cff','#cc5c5c','#35b6b4','#d7ff64','#a78bfa','#fb7185','#60a5fa','#f59e0b','#34d399'];
const MATERIAL_FILTERS = ['rule','supervisory_statement','statement_of_policy','definition','permission','legal_instrument','external_reference'];
const RELATIONSHIP_ORDER = TYPES;

function App(){
  const [q,setQ]=useState('');
  const [results,setResults]=useState([]);
  const [railContext,setRailContext]=useState(null);
  const [railStack,setRailStack]=useState([]);
  const [selected,setSelected]=useState(null);
  const [detail,setDetail]=useState(null);
  const [graph,setGraph]=useState({nodes:[],edges:[],available_edge_types:{}});
  const [contents,setContents]=useState({root:null,children:[]});
  const [representation,setRepresentation]=useState('combined');
  const [depth,setDepth]=useState(1);
  const [limit,setLimit]=useState(140);
  const [explicitOnly,setExplicitOnly]=useState(false);
  const [originFilter,setOriginFilter]=useState('all');
  const [types,setTypes]=useState(DEFAULT_TYPES);
  const [nodeTypes,setNodeTypes]=useState(new Set(NODE_TYPES));
  const [showInsurance,setShowInsurance]=useState(false);
  const [stats,setStats]=useState(null);
  const [view,setView]=useState('graph');
  const [validation,setValidation]=useState(null);
  const [panelOpen,setPanelOpen]=useState(()=>window.innerWidth>1400);
  const [graphExpanded,setGraphExpanded]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');

  const typesKey=useMemo(()=>[...types].sort().join('|'),[types]);

  useEffect(()=>{ bootstrap(); },[]);
  useEffect(()=>{ if(selected && !['whole_map','article_map'].includes(representation)) loadNeighbourhood(selected.id); },[selected?.id,depth,limit,explicitOnly,typesKey,representation]);

  async function api(path){
    const r=await fetch(API_BASE+path);
    if(!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
    return r.json();
  }
  async function bootstrap(){
    try{
      const [statsData,parts,roots]=await Promise.all([
        api('/stats'), api('/nodes?types=part&limit=300'), api('/nodes?types=rulebook&limit=1')
      ]);
      setStats(statsData);
      setResults(parts.results||[]);
      setRailContext(null);
      setRailStack([]);
      if(roots.results?.[0]) await choose(roots.results[0], {drill:false, openPanel:false});
    }catch(e){setError(e.message||String(e));}
  }
  async function showQuality(){
    setView('quality');
    setPanelOpen(false);
    if(validation) return;
    setBusy(true); setError('');
    try{ setValidation(await api('/validation/dashboard')); }
    catch(err){ setError(err.message||String(err)); }
    finally{ setBusy(false); }
  }
  async function loadAllParts(){
    const data=await api('/nodes?types=part&limit=300');
    setResults(data.results||[]);
    setRailContext(null);
    setRailStack([]);
  }
  async function search(e,first=false){
    e?.preventDefault(); setBusy(true); setError('');
    try{
      if(!q.trim()){
        await loadAllParts();
      }else{
        const data=await api(`/search?q=${encodeURIComponent(q)}&limit=30`);
        setResults(data.results||[]);
        setRailContext({kind:'Search results',title:q.trim()});
        setRailStack([]);
        if((first || !selected) && data.results?.[0]) await choose(data.results[0], {drill:false});
      }
    }catch(err){setError(err.message||String(err));}
    finally{setBusy(false);}
  }
  async function choose(n, opts={drill:true}){
    const full=await api(`/node/${n.id}`);
    setSelected(full); setDetail(full); setPanelOpen(opts.openPanel ?? window.innerWidth>1400);
    const [tree]=await Promise.all([loadContents(full.id), ['whole_map','article_map'].includes(representation)?Promise.resolve(null):loadNeighbourhood(full.id)]);
    if(opts.drill!==false && tree?.children?.length && ['rulebook','part','chapter','guidance_document','guidance_section'].includes(full.node_type)){
      setRailStack(stack=>[...stack,{results,railContext}]);
      setResults(tree.children);
      setRailContext({kind:'Contents',title:full.title});
    }
  }
  function goUp(){
    setRailStack(stack=>{
      if(!stack.length) return stack;
      const previous=stack[stack.length-1];
      setResults(previous.results||[]);
      setRailContext(previous.railContext||null);
      return stack.slice(0,-1);
    });
  }
  async function loadContents(id){
    try{
      const data=await api(`/node/${id}/contents`);
      setContents(data);
      return data;
    }catch{
      setContents({root:null,children:[]});
      return null;
    }
  }
  async function loadNeighbourhood(id){
    const effectiveLimit=Math.min(1000,Math.max(limit,limit*depth));
    const p=new URLSearchParams({depth:String(depth),limit:String(effectiveLimit),explicit_only:String(explicitOnly)});
    [...types].forEach(t=>p.append('edge_types',t));
    const data=await api(`/node/${id}/neighbourhood?${p}`);
    setGraph(data);
  }
  function applyRepresentation(key){
    if(key==='custom'){ setRepresentation('custom'); return; }
    const preset=REPRESENTATIONS[key]||REPRESENTATIONS.combined;
    setRepresentation(key);
    setTypes(new Set(preset.types));
    setDepth(preset.depth);
    setExplicitOnly(preset.explicitOnly);
  }
  function toggleType(t){ const next=new Set(types); next.has(t)?next.delete(t):next.add(t); setTypes(next); setRepresentation('custom'); }
  const activeRep=REPRESENTATIONS[representation]||{label:'Custom',hint:'Manual edge-type selection.'};
  const relationshipFilters=useMemo(()=>availableRelationshipTypes(stats,graph),[stats,graph]);
  const visibleGraph=useMemo(()=>filterGraph(graph,nodeTypes,types,originFilter,selected?.id,showInsurance),[graph,nodeTypes,typesKey,originFilter,selected?.id,showInsurance]);
  const selectedEdges=useMemo(()=>visibleGraph.edges.filter(e=>detail&&(e.from_node_id===detail.id||e.to_node_id===detail.id)),[visibleGraph,detail]);
  function toggleNodeType(t){
    const next=new Set(nodeTypes);
    const groups={
      rule:['rule','chapter','part','rulebook'],
      definition:['defined_term','glossary','crr_terms_list'],
      supervisory_statement:['guidance_document','guidance_section','guidance_paragraph'],
      statement_of_policy:['guidance_document','guidance_section','guidance_paragraph'],
      analysis:['topic','topic_cluster','obligation_pattern','obligation_statement'],
      permission:['permission'],
      legal_instrument:['legal_instrument'],
      external_reference:['external_reference','rule_reference'],
    };
    const group=groups[t]||[t];
    const allOn=group.every(x=>next.has(x));
    group.forEach(x=>allOn?next.delete(x):next.add(x));
    setNodeTypes(next);
  }

  return <div className={`${graphExpanded?'shell graph-expanded':'shell'} ${panelOpen?'panel-open':'panel-closed'} ${view==='quality'?'quality-view':''}`}>
    <header className="topbar">
      <a className="home" href="/">‹</a>
      <form className="command" onSubmit={search}>
        <span>⌕</span><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search, or leave blank for all Parts" autoFocus/><button>{busy?'…':'Search'}</button>
      </form>
      <div className="top-actions">
        <button className={view==='graph'?'mode on':'mode'} onClick={()=>setView('graph')}>Graph</button>
        <button className={view==='quality'?'mode on':'mode'} onClick={showQuality}>Quality</button>
        <button onClick={()=>setPanelOpen(!panelOpen)} title="Toggle side panel">◧</button>
        <details className="settings"><summary title="Display settings">⚙</summary><div className="settings-pop">
          <div className="filter-section representation-section"><h4>Representation</h4><div className="type-grid representation-grid">{Object.entries(REPRESENTATIONS).map(([key,preset])=><button type="button" key={key} className={representation===key?'on':''} onClick={()=>applyRepresentation(key)}><span>{preset.label}</span></button>)}<button type="button" className={representation==='custom'?'on':''} onClick={()=>applyRepresentation('custom')}><span>Custom</span></button></div><p className="rep-hint"><b>{activeRep.label}</b>{activeRep.hint}</p></div>
          <label className="depth-control"><span>Depth</span><input type="range" min="1" max="3" step="1" value={depth} onInput={e=>{setDepth(Number(e.currentTarget.value));setRepresentation('custom')}} onChange={e=>{setDepth(Number(e.currentTarget.value));setRepresentation('custom')}}/><b>{depth}</b><span className="stepper"><button type="button" onClick={()=>{setDepth(d=>Math.max(1,d-1));setRepresentation('custom')}}>−</button><button type="button" onClick={()=>{setDepth(d=>Math.min(3,d+1));setRepresentation('custom')}}>＋</button></span></label>
          <label>Node cap <input type="number" min="30" max="800" value={limit} onChange={e=>setLimit(Number(e.target.value))}/></label>
          <label className="check"><input type="checkbox" checked={explicitOnly} onChange={e=>{setExplicitOnly(e.target.checked);setRepresentation('custom')}}/> Direct links only when loading</label>
          <label className="check"><input type="checkbox" checked={showInsurance} onChange={e=>setShowInsurance(e.target.checked)}/> Insurance parts</label>
          <div className="filter-section"><h4>Link origin</h4><div className="type-grid origin-grid">{Object.entries(ORIGIN_FILTERS).map(([key,label])=><button type="button" key={key} className={originFilter===key?'on':''} onClick={()=>setOriginFilter(key)}><span>{label}</span></button>)}</div></div>
          <div className="filter-section"><h4>Material</h4><div className="type-grid material-grid">{MATERIAL_FILTERS.map(t=><button type="button" key={t} className={materialFilterOn(t,nodeTypes)?'on':''} onClick={()=>toggleNodeType(t)}><span>{materialLabel(t)}</span></button>)}</div></div>
          <div className="filter-section"><h4>Relationship edges</h4><div className="type-grid">{relationshipFilters.map(t=><button type="button" key={t} className={types.has(t)?'on':''} onClick={()=>toggleType(t)}><span>{relationLabel(t)}</span><em>{relationshipCount(t,stats,graph)}</em></button>)}</div></div>
        </div></details>
      </div>
    </header>

    <aside className="rail">
      <div className="product"><strong>PRA Rulebook</strong><span>{railContext?`${railContext.kind} · ${railContext.title}`:(q.trim()?'Search results':'All Rulebook Parts')} · {stats?`${stats.nodes.toLocaleString()} nodes`:''}</span><div className="rail-actions">{railStack.length>0&&<button className="back-link" onClick={goUp}>‹ Up one level</button>}{railContext&&<button className="back-link secondary" onClick={loadAllParts}>All Parts</button>}</div></div>
      {error&&<div className="error">{error}</div>}
      <div className="result-stack">{results.map(r=><button key={r.id} className={selected?.id===r.id?'hit active':'hit'} onClick={()=>choose(r)}><span>{label(r.node_type)}</span><strong><NodeTitle node={r}/></strong><small>{truncate(r.snippet||r.text,128)}</small></button>)}</div>
    </aside>

    <main className="canvas">
      {view==='quality'?<ValidationDashboard data={validation} busy={busy}/>:<>
        <div className="canvas-meta"><strong>{selected?.title||'Select a node'}</strong><span>{activeRep.label} · {visibleGraph.nodes.length} shown · {visibleGraph.edges.length} visible links · {Object.values(graph.available_edge_types||{}).reduce((a,b)=>a+b,0)} direct links available</span><button className="expand-graph" onClick={()=>setGraphExpanded(v=>!v)}>{graphExpanded?'Collapse graph':'Expand graph'}</button></div>
        <Graph graph={visibleGraph} selected={selected} detail={detail} nodeTypes={nodeTypes} relationshipTypes={types} relationshipFilters={relationshipFilters} availableEdgeTypes={graph.available_edge_types||{}} onToggleNodeType={toggleNodeType} onToggleRelationship={toggleType} onSelect={n=>{setDetail(n);setPanelOpen(true);}} onOpen={n=>choose(n,{drill:true})}/>
      </>}
    </main>

    <aside className={panelOpen?'inspector open':'inspector'}>
      <Explore node={detail} edges={selectedEdges} graph={graph} onChoose={choose}/>
    </aside>
  </div>;
}

function ValidationDashboard({data,busy}){
  const checks=data?.checks||[];
  const reporting=data?.reporting||null;
  const reportingIssue=useMemo(()=>reporting?reportingIssueConfig(reporting):null,[reporting]);
  const suspect403Issue=useMemo(()=>suspect403IssueConfig(data?.suspect_403_reference_samples||[]),[data]);
  const issues=useMemo(()=>[
    ...checks.map(c=>issueConfig(c.check,data,c)),
    ...(reportingIssue?[reportingIssue]:[]),
    ...(suspect403Issue?[suspect403Issue]:[]),
  ].sort((a,b)=>issueRank(b)-issueRank(a)),[checks,data,reportingIssue,suspect403Issue]);
  const activeIssues=issues.filter(i=>i.status!=='pass');
  const statusCounts=checks.reduce((acc,c)=>{acc[c.status]=(acc[c.status]||0)+1;return acc;},{});
  const [activeId,setActiveId]=useState('');
  const [detailTab,setDetailTab]=useState('samples');
  const [queueFilter,setQueueFilter]=useState('open');
  const [checksMinimised,setChecksMinimised]=useState(false);
  const [sampleQuery,setSampleQuery]=useState('');
  const [evidenceFilters,setEvidenceFilters]=useState({type:'all',evidence:'all',method:'all',minConfidence:0,query:''});
  const [reviewDraft,setReviewDraft]=useState('');
  const [auditState,setAuditState]=useState(()=>readAuditState());
  const [linkReviewChoices,setLinkReviewChoices]=useState({});

  useEffect(()=>{
    if(!issues.length) return;
    if(!activeId || !issues.some(i=>i.id===activeId)) setActiveId((activeIssues[0]||issues[0]).id);
  },[issues,activeIssues,activeId]);

  if(busy&&!data) return <section className="quality"><div className="canvas-meta"><strong>Quality</strong><span>Checking the full database…</span></div></section>;
  if(!data) return <section className="quality"><div className="canvas-meta"><strong>Quality</strong><span>Open Quality to load the audit.</span></div></section>;

  const activeIssue=issues.find(i=>i.id===activeId)||activeIssues[0]||issues[0];
  const reviewedCount=issues.filter(i=>auditState[i.id]?.status==='reviewed').length;
  const acceptedCount=issues.filter(i=>auditState[i.id]?.status==='accepted').length;
  const riskScore=issues.reduce((sum,i)=>sum+issueRank(i),0);
  const visibleSamples=filterRows(activeIssue?.rows||[],sampleQuery);
  const evidenceRows=filterEvidenceRows(data.edges_by_type_method||[],evidenceFilters);
  const queue=issues.filter(i=>{
    const state=auditState[i.id]?.status||'open';
    if(queueFilter==='all') return true;
    if(queueFilter==='reporting') return i.reporting;
    if(queueFilter==='passing') return i.status==='pass';
    if(queueFilter==='accepted') return state==='accepted';
    return i.status!=='pass' && state!=='accepted';
  });
  const nextIssue=activeIssues.find(i=>(auditState[i.id]?.status||'open')!=='accepted')||activeIssues[0]||issues[0];
  const posture=statusCounts.fail?'fail':statusCounts.warn?'warn':'pass';

  function openIssue(issue,tab='samples'){
    setActiveId(issue.id);
    setDetailTab(tab);
    setSampleQuery('');
  }

  return <section className="quality audit-cockpit">
    <div className="canvas-meta"><strong>Quality</strong><span className={`quality-state ${posture}`}>{statusCounts.fail||0} fail · {statusCounts.warn||0} warn · {statusCounts.pass||0} pass</span></div>

    <div className="audit-strip">
      <div className="audit-strip-main">
        <span>Next check</span>
        <strong>{nextIssue?.title||'No checks'}</strong>
        <p>{nextIssue?.summary||'No validation data available.'}</p>
      </div>
      <div className="audit-strip-stats">
        <div><span>Risk</span><strong>{fmt(riskScore)}</strong></div>
        <div><span>Open</span><strong>{fmt(activeIssues.filter(i=>(auditState[i.id]?.status||'open')!=='accepted').length)}</strong></div>
        <div><span>Reviewed</span><strong>{fmt(reviewedCount)}</strong></div>
        <div><span>Accepted</span><strong>{fmt(acceptedCount)}</strong></div>
      </div>
      <button type="button" onClick={()=>downloadCsv('audit-evidence.csv',data.edges_by_type_method||[])}>Export evidence</button>
    </div>

    <div className={`audit-layout ${checksMinimised?'checks-minimised':''}`}>
      <aside className={`audit-queue ${checksMinimised?'minimised':''}`} aria-label="Quality checks">
        {checksMinimised?<button type="button" className="checks-rail-tab" onClick={()=>setChecksMinimised(false)} title="Expand checks"><span>Checks</span><strong>{queue.length}</strong></button>:<>
        <div className="queue-tools">
          <div>
            <strong>Checks</strong>
            <span>{fmt(data.totals?.nodes)} nodes · {fmt(data.totals?.edges)} links</span>
          </div>
          <button type="button" className="minimise-checks" onClick={()=>setChecksMinimised(true)} title="Minimise checks">‹</button>
          <div className="queue-filter" role="tablist" aria-label="Filter checks">
            {['open','reporting','all','passing','accepted'].map(f=><button key={f} type="button" className={queueFilter===f?'on':''} onClick={()=>setQueueFilter(f)}>{f}</button>)}
          </div>
        </div>
        <div className="queue-list">
          {queue.length?queue.map(issue=><IssueQueueButton key={issue.id} issue={issue} active={activeIssue?.id===issue.id} state={auditState[issue.id]} onOpen={()=>openIssue(issue)} />):<p className="empty-good">Nothing in this queue.</p>}
        </div>
        </>}
      </aside>

      <main className="audit-detail" aria-label="Selected quality check">
        {activeIssue&&<>
          <div className={`detail-hero ${activeIssue.status}`}>
            <div>
              <span>{activeIssue.severity}</span>
              <h2>{activeIssue.title}</h2>
              <p>{activeIssue.summary}</p>
            </div>
            <div className="detail-metrics">
              <div><span>Affected</span><strong>{fmt(activeIssue.affected)}</strong></div>
              <div><span>Priority</span><strong>{fmt(issueRank(activeIssue))}</strong></div>
              <div><span>State</span><strong>{auditState[activeIssue.id]?.status||'open'}</strong></div>
            </div>
          </div>

          <div className="detail-cards">
            <div><span>Impact</span><p>{activeIssue.impact}</p></div>
            <div><span>Likely cause</span><p>{activeIssue.cause}</p></div>
            <div><span>Pass condition</span><p>{activeIssue.test}</p></div>
          </div>

          <div className="detail-tabs" role="tablist" aria-label="Issue tools">
            {[['samples','Samples'],['fix','Fix plan'],['evidence','Evidence'],['review','Notes']].map(([key,label])=><button key={key} type="button" className={detailTab===key?'on':''} onClick={()=>setDetailTab(key)}>{label}</button>)}
            <button type="button" className="ghost" onClick={()=>setIssueState(activeIssue.id,'reviewed',setAuditState)}>Mark reviewed</button>
            <button type="button" className="ghost" onClick={()=>setIssueState(activeIssue.id,'accepted',setAuditState)}>Accept warning</button>
          </div>

          {detailTab==='samples'&&<div className="detail-panel">
            {activeIssue.reporting?<ReportingProspectiveIssues issue={activeIssue}/>:activeIssue.id==='suspect-403-links'?<Suspect403Review issue={activeIssue} choices={linkReviewChoices} setChoices={setLinkReviewChoices}/>:activeIssue.id==='unresolved-references'?<UnresolvedReferencePatterns issue={activeIssue} visibleSamples={visibleSamples} sampleQuery={sampleQuery} setSampleQuery={setSampleQuery}/>:<>
              <div className="sample-toolbar compact"><input value={sampleQuery} onChange={e=>setSampleQuery(e.target.value)} placeholder="Filter sample rows…"/><button onClick={()=>downloadCsv(`${activeIssue.id}-samples.csv`,visibleSamples)}>Export samples</button></div>
              <QualityTable title={activeIssue.sampleTitle} rows={visibleSamples.slice(0,activeIssue.sampleLimit||100)} cols={activeIssue.cols}/>
            </>}
          </div>}

          {detailTab==='fix'&&<div className="detail-panel">{activeIssue.reporting?<ReportingFixPlan issue={activeIssue}/>:<FixPlan issue={activeIssue}/>}</div>}

          {detailTab==='evidence'&&<div className="detail-panel evidence-workbench">
            {activeIssue.reporting?<QualityTable title="Reporting relationship evidence" rows={(activeIssue.reportingData.edges_by_type_method||[]).slice(0,120)} cols={['edge_type','extraction_method','review_status','edges','avg_confidence','min_confidence','max_confidence']}/>:<>
              <EvidenceFilters rows={data.edges_by_type_method||[]} filters={evidenceFilters} setFilters={setEvidenceFilters}/>
              <QualityTable rows={evidenceRows.slice(0,120)} cols={['edge_type','source_method','evidence_status','edges','avg_confidence','min_confidence','max_confidence']}/>
            </>}
          </div>}

          {detailTab==='review'&&<div className="detail-panel review-editor inline-review">
            <div className="review-actions"><button onClick={()=>setIssueState(activeIssue.id,'reviewed',setAuditState)}>Mark reviewed</button><button onClick={()=>setIssueState(activeIssue.id,'accepted',setAuditState)}>Accept warning</button><button onClick={()=>setIssueState(activeIssue.id,'open',setAuditState)}>Reopen</button></div>
            <textarea value={reviewDraft} onChange={e=>setReviewDraft(e.target.value)} placeholder="Add reviewer note…"/>
            <button onClick={()=>{appendIssueNote(activeIssue.id,reviewDraft,setAuditState);setReviewDraft('');}}>Save note</button>
            <ReviewNotes notes={auditState[activeIssue.id]?.notes||[]}/>
          </div>}
        </>}
      </main>
    </div>
  </section>;
}

function UnresolvedReferencePatterns({issue,visibleSamples,sampleQuery,setSampleQuery}){
  const queues=useMemo(()=>buildUnresolvedActionQueues(visibleSamples),[visibleSamples]);
  const [activeQueue,setActiveQueue]=useState('all');
  useEffect(()=>{
    if(activeQueue==='all') return;
    if(!queues.some(q=>q.id===activeQueue)) setActiveQueue('all');
  },[queues,activeQueue]);
  const selectedQueue=queues.find(q=>q.id===activeQueue);
  const workingRows=activeQueue==='all'?visibleSamples.map(r=>({next_action:'Review pattern',why:'Choose an action queue or use the pattern cards to decide the next step.',...r})):selectedQueue?.rows||[];
  const cols=['next_action','why','target_type','target_title','source_title','source_text','source_url','confidence'];
  return <div className="unresolved-patterns workflow-mode">
    <div className="workflow-intro">
      <div><strong>What to do next</strong><p>Pick an action queue. Each unresolved link is labelled with the decision Andrew needs to make, rather than just shown as a raw sample row.</p></div>
      <button onClick={()=>downloadCsv(`${issue.id}-${activeQueue}-workflow.csv`,workingRows)}>Export current queue</button>
    </div>
    <div className="action-queue-grid" role="tablist" aria-label="Unresolved link action queues">
      <button type="button" className={activeQueue==='all'?'action-card on':'action-card'} onClick={()=>setActiveQueue('all')}><span>{fmt(visibleSamples.length)} rows</span><strong>All unresolved</strong><em>Search and inspect everything</em></button>
      {queues.map(q=><button key={q.id} type="button" className={activeQueue===q.id?'action-card on':'action-card'} onClick={()=>setActiveQueue(q.id)}><span>{fmt(q.count)} rows</span><strong>{q.label}</strong><em>{q.helper}</em></button>)}
    </div>
    <div className="pattern-grid compact-patterns">{(issue.patterns||[]).map(p=><article key={p.pattern} className="pattern-card"><div><span>{fmt(p.targets)} targets</span><strong>{p.pattern}</strong><em>{fmt(p.live_edges)} live links</em></div><ul>{(p.examples||[]).slice(0,3).map(ex=><li key={ex.target_id}><b>{ex.target_title}</b><small>{ex.example_source_title||ex.stable_key}</small></li>)}</ul></article>)}</div>
    <div className="sample-toolbar compact"><input value={sampleQuery} onChange={e=>setSampleQuery(e.target.value)} placeholder="Filter unresolved links by title, source, URL or action…"/><button onClick={()=>downloadCsv(`${issue.id}-samples.csv`,visibleSamples)}>Export all filtered</button></div>
    <QualityTable title={activeQueue==='all'?'Unresolved links: all action types':`${selectedQueue?.label||'Selected'} queue`} rows={workingRows.slice(0,issue.sampleLimit||100)} cols={cols}/>
  </div>;
}

function Suspect403Review({issue,choices,setChoices}){
  async function submit(row){
    const draft=choices[row.target_id]||{};
    const decision=draft.decision||row.review_decision||'';
    if(!decision) return alert('Choose valid, broken, needs URL fix, or unsure first.');
    const res=await fetch(API_BASE+'/validation/suspect-403-review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_id:row.target_id,review_id:row.review_id,decision,note:draft.note||row.review_note||''})});
    if(!res.ok) return alert(await res.text());
    setChoices(prev=>({...prev,[row.target_id]:{...(prev[row.target_id]||{}),saved:decision,decision}}));
  }
  const setDraft=(row,patch)=>setChoices(prev=>({...prev,[row.target_id]:{decision:row.review_decision||'',note:row.review_note||'',...(prev[row.target_id]||{}),...patch}}));
  return <section className="suspect-review"><div className="sample-toolbar compact"><strong>{fmt(issue.rows.length)} links to review</strong><span>Click the URL, choose an outcome, then submit so the decision is saved server-side.</span></div><div className="table-wrap"><table><thead><tr><th>Review ID</th><th>Current</th><th>Title</th><th>URL</th><th>Outcome</th><th>Note</th><th>Submit</th></tr></thead><tbody>{issue.rows.map(row=>{const draft=choices[row.target_id]||{}; const decision=draft.decision||row.review_decision||''; const saved=draft.saved||row.review_decision||''; return <tr key={row.target_id}><td>{row.review_id}</td><td>{saved||'—'}</td><td title={row.title}>{cell(row.title,'title')}</td><td title={row.url}><a className="table-link full-url" href={row.url} target="_blank" rel="noopener noreferrer">{row.url}</a></td><td><div className="decision-buttons">{['valid','broken','needs_url_fix','unsure'].map(v=><button key={v} type="button" className={decision===v?'on':''} onClick={()=>setDraft(row,{decision:v})}>{v.replaceAll('_',' ')}</button>)}</div></td><td><input value={draft.note??row.review_note??''} onChange={e=>setDraft(row,{note:e.target.value})} placeholder="Optional note or replacement URL"/></td><td><button type="button" onClick={()=>submit(row)}>Submit</button></td></tr>})}</tbody></table></div></section>;
}

function ReportingProspectiveIssues({issue}){
  const samples=issue.reportingData.samples||{};
  return <div className="reporting-detail">
    <div className="reporting-prospects">
      {issue.prospectiveIssues.map(p=><article key={p.check} className={`reporting-prospect ${p.status}`}><span>{statusIcon(p.status)} {plainCheckName(p.check)}</span><p>{p.purpose}</p><div>{Object.entries(p.metrics||{}).map(([k,v])=><em key={k}><b>{metricLabel(k)}</b>{fmt(v)}</em>)}</div></article>)}
    </div>
    <div className="reporting-sample-grid">
      <QualityTable title="Data items without templates" rows={(samples.data_items_without_templates||[]).slice(0,40)} cols={['node_id','label','source_table','source_pk']}/>
      <QualityTable title="Data items without source documents" rows={(samples.data_items_without_source_documents||[]).slice(0,40)} cols={['node_id','label','source_table','source_pk']}/>
      <QualityTable title="Templates without datapoints" rows={(samples.templates_without_datapoints||[]).slice(0,40)} cols={['node_id','label','source_table','source_pk']}/>
    </div>
  </div>;
}

function ReportingFixPlan({issue}){
  return <div className="fix-plan"><div className="fix-summary"><h3>Prospective reporting issues</h3><p>Use these as candidate quality gates for the reporting tables and reporting-rule links. Promote stable checks into hard failures once the expected coverage baseline is agreed.</p></div><ol>{issue.prospectiveIssues.map(p=><li key={p.check}><strong>{plainCheckName(p.check)}</strong><p>{p.purpose}</p></li>)}</ol><div className="acceptance"><span>Done when</span><p>{issue.test}</p></div></div>;
}

function IssueQueueButton({issue,active,state,onOpen}){
  const triage=state?.status||'open';
  return <button type="button" className={`queue-item ${issue.status} ${active?'on':''}`} onClick={onOpen}>
    <span className="queue-status">{statusIcon(issue.status)}</span>
    <span className="queue-copy"><strong>{issue.title}</strong><em>{issue.summary}</em></span>
    <span className="queue-meta"><b>{fmt(issue.affected)}</b><small>{triage}</small></span>
  </button>;
}

function IssueCard({issue,active,state,onOpen,onState}){
  return <article className={`work-issue ${issue.status} ${active?'active':''}`}>
    <div className="work-title"><span>{statusIcon(issue.status)}</span><div><strong>{issue.title}</strong><em>{issue.summary}</em></div><b>{issue.severity}</b></div>
    <div className="work-metrics"><div><span>Affected</span><strong>{fmt(issue.affected)}</strong></div><div><span>Priority</span><strong>{fmt(issueRank(issue))}</strong></div><div><span>State</span><strong>{state?.status||'open'}</strong></div></div>
    <p>{issue.impact}</p>
    <div className="work-actions"><button onClick={onOpen}>Inspect</button><button onClick={()=>onState('reviewed')}>Reviewed</button><button onClick={()=>onState('accepted')}>Accept</button></div>
  </article>;
}

function EvidenceFilters({rows,filters,setFilters}){
  const types=unique(rows.map(r=>r.edge_type));
  const evidence=unique(rows.map(r=>r.evidence_status));
  const methods=unique(rows.map(r=>r.source_method));
  return <div className="evidence-filters"><input value={filters.query} onChange={e=>setFilters({...filters,query:e.target.value})} placeholder="Search type, method or evidence…"/><select value={filters.type} onChange={e=>setFilters({...filters,type:e.target.value})}><option value="all">All link types</option>{types.map(v=><option key={v} value={v}>{relationLabel(v)}</option>)}</select><select value={filters.evidence} onChange={e=>setFilters({...filters,evidence:e.target.value})}><option value="all">All evidence kinds</option>{evidence.map(v=><option key={v} value={v}>{metricLabel(v)}</option>)}</select><select value={filters.method} onChange={e=>setFilters({...filters,method:e.target.value})}><option value="all">All methods</option>{methods.map(v=><option key={v} value={v}>{v}</option>)}</select><label>Min confidence <input type="range" min="0" max="1" step="0.05" value={filters.minConfidence} onChange={e=>setFilters({...filters,minConfidence:Number(e.target.value)})}/><b>{Math.round(filters.minConfidence*100)}%</b></label></div>;
}

function FixPlan({issue}){
  return <div className="fix-plan"><div className="fix-summary"><h3>{issue.title}</h3><p>{issue.fix}</p></div><ol>{issue.runbook.map((step,i)=><li key={i}><strong>{step.title}</strong><p>{step.text}</p>{step.command&&<code>{step.command}</code>}</li>)}</ol><div className="acceptance"><span>Done when</span><p>{issue.test}</p></div></div>;
}
function ReviewNotes({notes}){return <div className="review-notes">{notes.length?notes.map((n,i)=><p key={i}><b>{new Date(n.ts).toLocaleString()}</b>{n.text}</p>):<p className="muted">No notes yet.</p>}</div>}

function QualityTable({title,rows,cols}){
  const tableClass=title==='Live unresolved reference samples'?'quality-table unresolved-reference-table':'quality-table';
  return <section className={tableClass}>{title&&<h3>{title}</h3>}<div className="table-wrap"><table><thead><tr>{cols.map(c=><th key={c}>{metricLabel(c)}</th>)}</tr></thead><tbody>{rows.length?rows.map((r,i)=><tr key={i}>{cols.map(c=><td key={c} title={String(r[c]??'')}>{cell(r[c],c)}</td>)}</tr>):<tr><td colSpan={cols.length}>No rows</td></tr>}</tbody></table></div></section>;
}

function cell(v,key){
  if(v===null||v===undefined||v==='') return '—';
  if(key?.includes('confidence')&&typeof v==='number') return <span className={`confidence ${v<.6?'low':v<.8?'mid':'high'}`}><i style={{width:`${Math.max(3,Math.min(100,v*100))}%`}} />{Math.round(v*100)}%</span>;
  if(typeof v==='number') return fmt(v);
  const s=String(v);
  if(key==='source_url'&&s.startsWith('http')) return <a className="table-link full-url" href={s} target="_blank" rel="noopener noreferrer">{s}</a>;
  if((key==='url'||key==='target_url')&&s.startsWith('http')) return <a className="table-link" href={s} target="_blank" rel="noopener noreferrer">{compactUrl(s)}</a>;
  return s.length>120?s.slice(0,117)+'…':s;
}

function suspect403IssueConfig(rows){
  if(!rows.length) return null;
  return {
    id:'suspect-403-links',
    check:'suspect 403 links',
    status:'warn',
    metrics:{suspect_403_links:rows.length, affected_references:rows.reduce((a,r)=>a+(Number(r.live_edges)||0),0)},
    title:'403 link review',
    severity:'high',
    affected:rows.length,
    cols:['review_id','status','title','url','live_edges','target_id'],
    rows,
    sampleLimit:500,
    sampleTitle:'403 links for manual review',
    summary:`${fmt(rows.length)} external-reference URLs returned HTTP 403 to automated checks.`,
    impact:'These may be valid links blocked to scripts, or genuinely unavailable links hidden behind access controls.',
    cause:'Most 403s are BoE/PRA media or reporting URLs. The HTTP checker cannot distinguish bot protection from real access failure.',
    fix:'Open a sample in a normal browser. If it loads, mark as blocked-but-valid or update the URL if redirected. If it fails for a human too, classify it as a broken reference.',
    test:'Each 403 review row is either accepted as blocked-but-valid, corrected to a working URL, or classified as a broken reference.',
    runbook:[
      {title:'Open the review id',text:'Use the review id, e.g. 403-0042, to identify the exact target_id and URL in the table.'},
      {title:'Check manually',text:'Open the URL in a normal browser session. Record whether it loads, redirects to a better canonical URL, or fails.'},
      {title:'Apply outcome',text:'For valid blocked links, label as externally blocked/valid. For dead links, label as Broken reference. For redirects, update the external reference URL.'},
    ],
  };
}

function reportingIssueConfig(reporting){
  const checks=reporting.checks||[];
  const totals=reporting.totals||{};
  const status=checks.some(c=>c.status==='fail')?'fail':checks.some(c=>c.status==='warn')?'warn':'pass';
  const affected=checks.flatMap(c=>Object.values(c.metrics||{})).filter(v=>typeof v==='number').reduce((a,b)=>Math.max(a,b),0);
  const prospectiveIssues=checks.map(c=>({
    ...c,
    affected:Object.values(c.metrics||{}).filter(v=>typeof v==='number').reduce((a,b)=>Math.max(a,b),0),
  }));
  return {
    id:'reporting-rules',
    check:'reporting rules',
    reporting:true,
    reportingData:reporting,
    prospectiveIssues,
    status,
    metrics:totals,
    title:'Reporting rules',
    severity:status==='fail'?'critical':status==='warn'?'high':'passed',
    affected,
    cols:['status','issue','affected','purpose'],
    rows:prospectiveIssues,
    sampleTitle:'Prospective reporting issues',
    summary:`${fmt(totals.data_items||0)} data items, ${fmt(totals.templates||0)} templates, ${fmt(totals.datapoints||0)} datapoints and ${fmt(totals.reporting_reference_edges||0)} reporting-rule reference links.`,
    impact:'Reporting links may be incomplete or insufficiently evidenced if these candidate checks are not monitored.',
    cause:'The reporting dataset has separate tables and graph edges from the core Rulebook graph, so it needs its own prospective coverage checks.',
    fix:'Review the prospective reporting issues, decide which are expected gaps, then promote agreed checks into hard validation gates.',
    test:'Reporting appears as one Checks-panel item and exposes candidate coverage, evidence and resolution issues without a separate dashboard section.',
    runbook:[{title:'Review prospective issues',text:'Check the reporting coverage, reference evidence and resolution checks against the database baseline.'}],
  };
}

function issueConfig(check,data,raw={check,status:'pass',metrics:{}}){
  const metrics=raw.metrics||{};
  const base={id:check.replace(/[^a-z0-9]+/gi,'-').replace(/^-|-$/g,'').toLowerCase(),check,status:raw.status||'pass',metrics,title:plainCheckName(check),severity:raw.status==='fail'?'critical':raw.status==='warn'?'high':'passed',affected:Object.values(metrics).filter(v=>typeof v==='number').reduce((a,b)=>Math.max(a,b),0),cols:['title'],rows:[],sampleTitle:'Samples',summary:checkPlainEnglish(raw),impact:'No active issue.',cause:'The check is passing.',fix:'Keep this check in the regression set.',test:'Validation remains green after the next extraction run.',runbook:[{title:'Rerun validation',text:'Confirm this check remains stable after data or parser changes.',command:'python scripts/validate_edge_evidence.py'}]};
  if(check==='self-loops / near-self-loops') return {...base,severity:'high',affected:metrics.near_self_loop_sample_rows||0,summary:'Potential duplicate node identities expressed as relationships.',impact:'Can make the graph invent relationships between what is actually the same legal paragraph.',cause:'Extractor canonicalisation mismatch between paragraph keys, HTML ids, aliases or roll-up nodes.',fix:'Inspect candidate pairs, merge genuine duplicates, preserve aliases, and leave appendix paragraph-number collisions separate.',test:'Near-self-loop samples fall to zero or are explainable retained cases; exact duplicate and paragraph/HTML duplicate checks remain zero.',sampleTitle:'Near-self-loop samples',rows:(data.near_self_loop_samples||[]),cols:['edge_type','source_method','node_type','title'],runbook:[{title:'Inspect samples',text:'Start with repeated titles and source methods to separate true duplicates from legitimate appendix repeats.'},{title:'Patch canonicalisation',text:'Merge genuine duplicate nodes while preserving legacy aliases and source URLs.',command:'python scripts/fix_guidance_duplicate_aliases_fast.py'},{title:'Validate evidence',text:'Rerun edge evidence checks and refresh the dashboard.',command:'python scripts/validate_edge_evidence.py'}]};
  if(check==='unresolved references') return {...base,severity:'high',affected:metrics.live_placeholder_reference_nodes||0,summary:'Live references still point to placeholder targets rather than resolved legal nodes.',impact:'Users can see that a provision references something, but cannot reliably navigate to the target law.',cause:'The remaining set is now mostly internal PRA rule/guidance shorthand plus a smaller number of generic anchors and raw URLs.',fix:'Use the Samples tab as an action workflow: resolve internal Rulebook-looking rows, check raw URLs, inspect generic link text, and classify true external references without forcing them into Rulebook nodes.',test:'Each unresolved row has a clear next action and the live placeholder count reduces without an increase in bad matches.',sampleTitle:'Live unresolved reference workflow',patterns:data.unresolved_reference_patterns||[],rows:(data.unresolved_reference_samples||[]),sampleLimit:2000,cols:['next_action','why','target_type','target_title','source_title','source_text','source_url','confidence'],runbook:[{title:'Pick an action queue',text:'Start with Resolve internally for Rulebook-looking rows, then Check URL and Inspect context.'},{title:'Resolve Rulebook-looking placeholders',text:'Use title, part date, article and paragraph context to map placeholders to stable nodes.',command:'python scripts/patch_unresolved_reference_patterns.py'},{title:'Classify true externals',text:'Do not force external documents into Rulebook nodes. Keep them explicit as labelled external references.'}]};
  if(check==='missing evidence/source URL') return {...base,severity:raw.status==='pass'?'passed':'critical',affected:(metrics.missing_evidence_text||0)+(metrics.missing_source_url||0),summary:'Relationships without auditable provenance.',impact:'Hard legal links cannot be trusted unless a user can inspect source URL, method and evidence text.',cause:'Backfill or importer path missed evidence metadata on one or more edge types.',fix:'Backfill missing source URL/evidence text and block hard legal edges without provenance.',test:'All missing evidence/source metrics are zero.',runbook:[{title:'Backfill metadata',text:'Fill source URL, method, confidence, extraction run and evidence text.',command:'python scripts/backfill_edge_evidence.py'},{title:'Validate',text:'Run the strict evidence validation gate.',command:'python scripts/validate_edge_evidence.py'}]};
  if(check==='duplicate logical nodes') return {...base,severity:raw.status==='pass'?'passed':'critical',affected:(metrics.exact_html_duplicate_groups||0)+(metrics.paragraph_vs_html_id_key_pairs||0),summary:'The same paragraph may exist as more than one node.',impact:'Duplicate legal nodes contaminate navigation, counts and derived relationship evidence.',cause:'Stable-key generation or alias merging failed for some guidance/rule paragraphs.',fix:'Merge duplicate identities and preserve aliases.',test:'Duplicate groups and paragraph/HTML-id key pairs are zero.',runbook:[{title:'Run duplicate alias fixer',text:'Apply canonical guidance node merge rules.',command:'python scripts/fix_guidance_duplicate_aliases_fast.py'},{title:'Audit canonical views',text:'Regenerate canonical views if parser logic changed.',command:'python scripts/create_canonical_guidance_views.py'}]};
  if(check==='hard vs soft edge split') return {...base,severity:'passed',affected:metrics.hard_explicit_edges||0,summary:'Confirms direct legal links are distinguishable from inferred analytical links.',impact:'Prevents inferred similarity/roll-up edges being mistaken for legal proof.',cause:'N/A, this split currently passes.',fix:'Keep UI labels and API filters explicit: direct/source-backed versus inferred/derived.',test:'Hard and soft edge counts remain separately reported.',runbook:[{title:'Regression check',text:'Confirm new importers populate evidence_status consistently.',command:'python scripts/validate_edge_evidence.py'}]};
  return base;
}
function issueRank(i){if(i.status==='pass') return 0; const sev={critical:100,high:60,medium:30,low:10}[i.severity]||20; return sev+Math.min(50,Math.log10((i.affected||0)+1)*18)}
function filterRows(rows,q){const needle=q.trim().toLowerCase(); if(!needle) return rows; return rows.filter(r=>Object.values(r).some(v=>String(v??'').toLowerCase().includes(needle)))}
function filterEvidenceRows(rows,f){return rows.filter(r=>(f.type==='all'||r.edge_type===f.type)&&(f.evidence==='all'||r.evidence_status===f.evidence)&&(f.method==='all'||r.source_method===f.method)&&((r.min_confidence??0)>=f.minConfidence)&&(!f.query||Object.values(r).some(v=>String(v??'').toLowerCase().includes(f.query.toLowerCase())))).sort((a,b)=>(a.min_confidence??1)-(b.min_confidence??1)||(b.edges||0)-(a.edges||0))}
function unique(xs){return [...new Set(xs.filter(Boolean))].sort()}
function readAuditState(){try{return JSON.parse(localStorage.getItem('pra-rulebook-audit-state')||'{}')}catch{return {}}}
function writeAuditState(next){localStorage.setItem('pra-rulebook-audit-state',JSON.stringify(next));return next}
function setIssueState(id,status,setter){setter(prev=>writeAuditState({...prev,[id]:{...(prev[id]||{}),status,updated_at:new Date().toISOString()}}))}
function appendIssueNote(id,text,setter){if(!text.trim()) return; setter(prev=>writeAuditState({...prev,[id]:{...(prev[id]||{}),notes:[...((prev[id]||{}).notes||[]),{text:text.trim(),ts:Date.now()}],updated_at:new Date().toISOString()}}))}
function downloadCsv(filename,rows){const list=rows||[]; const cols=unique(list.flatMap(r=>Object.keys(r||{}))); const esc=v=>`"${String(v??'').replaceAll('"','""')}"`; const csv=[cols.join(','),...list.map(r=>cols.map(c=>esc(r[c])).join(','))].join('\n'); const blob=new Blob([csv],{type:'text/csv'}); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=filename; a.click(); URL.revokeObjectURL(a.href)}
function fmt(v){return typeof v==='number'?v.toLocaleString(undefined,{maximumFractionDigits:3}):v}
function human(s){return String(s).replace(/_/g,' ')}
function compactUrl(value){try{const u=new URL(value); const path=u.pathname.length>38?u.pathname.slice(0,35)+'…':u.pathname; return `${u.hostname}${path}`;}catch{return value.length>80?value.slice(0,77)+'…':value}}
function statusIcon(s){return s==='pass'?'✓':s==='warn'?'!':'×'}
function plainCheckName(s){return ({'duplicate logical nodes':'Duplicate paragraphs','missing evidence/source URL':'Evidence coverage','self-loops / near-self-loops':'Loop checks','unresolved references':'Unresolved links','hard vs soft edge split':'Hard vs soft links','suspect 403 links':'403 link review','reporting coverage':'Reporting coverage','reporting reference evidence':'Reference evidence','reporting reference resolution':'Reference resolution'}[s]||s)}
function checkPlainEnglish(c){
  if(c.check==='duplicate logical nodes') return 'Looks for the same paragraph appearing twice.';
  if(c.check==='missing evidence/source URL') return 'Checks every link has a reason and a source.';
  if(c.check==='self-loops / near-self-loops') return 'Finds links that may really point back to the same item.';
  if(c.check==='unresolved references') return 'Counts links that still need matching to real Rulebook nodes.';
  if(c.check==='hard vs soft edge split') return 'Separates direct legal links from analytical suggestions.';
  if(c.check==='suspect 403 links') return 'Manual review queue for external URLs blocked during automated link checks.';
  return c.purpose||'';
}
function metricLabel(k){return ({exact_html_duplicate_groups:'Exact duplicate groups',paragraph_vs_html_id_key_pairs:'Paragraph/HTML-id duplicates',ambiguous_doc_paragraph_groups_not_auto_merged:'Repeated paragraph numbers kept separate',missing_source_method:'Missing method',missing_confidence:'Missing confidence',missing_source_url:'Missing source URL',missing_evidence_text:'Missing evidence text',missing_extraction_run_id:'Missing run id',missing_evidence_status:'Missing evidence status',hard_edges_missing_evidence_or_url:'Hard links missing evidence',self_loops:'Self-loops',near_self_loop_sample_rows:'Near-self-loop examples',near_self_loop_sample_capped:'More examples exist',placeholder_reference_nodes:'Placeholder references',live_placeholder_reference_nodes:'Live unresolved targets',all_placeholder_reference_nodes:'All placeholder targets',orphan_placeholder_reference_nodes:'Stale/orphan placeholder targets',external_reference:'External references',rule_reference:'Rule references',hard_explicit_edges:'Hard links',soft_inferred_edges:'Soft links',evidence_status_direct_text:'Direct text evidence',evidence_status_document_metadata:'Document metadata evidence',evidence_status_html_structure:'HTML structure evidence',evidence_status_inferred:'Inferred evidence',edge_type:'Link type',source_method:'How found',extraction_method:'Extraction method',review_status:'Review status',evidence_status:'Evidence kind',edges:'Links',avg_confidence:'Average confidence',min_confidence:'Lowest confidence',max_confidence:'Highest confidence',sample_id:'Sample ID',review_id:'Review ID',suspect_403_links:'403 links',affected_references:'Affected references',node_type:'Node type',node_id:'Node ID',label:'Label',source_table:'Source table',source_pk:'Source key',title:'Title',target_id:'Target ID',target_type:'Target type',target_title:'Unresolved target',target_url:'Target URL',source_type:'Source type',source_title:'Source provision',source_text:'Original provision text',source_container_title:'Container',original_source_method:'Original method',source_url:'Source URL',degree:'Total links',out_degree:'Outgoing links',in_degree:'Incoming links',url:'URL',normative_force:'Force',obligations:'Obligations',pct:'Share',data_items:'Data items',templates:'Templates',datapoints:'Datapoints',source_documents:'Source documents',reporting_reference_edges:'Reporting references',data_items_without_templates:'Data items without templates',data_items_without_source_documents:'Data items without source documents',templates_without_datapoints:'Templates without datapoints',obligations_without_data_item_node:'Obligations without data item',missing_evidence_span:'Missing evidence span',low_confidence_under_60pct:'Low confidence <60%',extracted_references:'Extracted references',unresolved_references:'Unresolved references',resolved_without_added_edge:'Resolved without link'}[k]||human(k))}

function Graph({graph,selected,detail,nodeTypes,relationshipTypes,relationshipFilters,availableEdgeTypes,onToggleNodeType,onToggleRelationship,onSelect,onOpen}){
  const fgRef=useRef(null);
  const wrapRef=useRef(null);
  const lastClickRef=useRef({id:null,time:0});
  const [hover,setHover]=useState(null);
  const [hoverEdge,setHoverEdge]=useState(null);
  const data=useMemo(()=>forceGraphData(graph,selected),[graph,selected?.id]);

  useEffect(()=>{
    const fg=fgRef.current;
    if(!fg) return;
    fg.d3Force('collide',forceCollide(node=>Math.max(22,node.size||22)+10).strength(.88));
    fg.d3Force('charge')?.strength(-260);
    fg.d3Force('link')?.distance(edge=>edge.edge_type==='contains'?90:160).strength(edge=>edge.edge_type==='contains'?.45:.12);
    const id=detail?.id||selected?.id;
    const node=id?data.nodes.find(n=>n.id===id):null;
    setTimeout(()=>node?frameNode(fg,node,420):fg.zoomToFit(420,70),260);
  },[data,detail?.id,selected?.id]);

  useEffect(()=>{
    const fg=fgRef.current;
    if(!fg) return;
    const id=detail?.id||selected?.id;
    if(!id) return;
    const node=data.nodes.find(n=>n.id===id);
    if(node) frameNode(fg,node,420);
  },[detail?.id,selected?.id,data]);

  function frameNode(fg,node,duration=360){
    fg.centerAt(node.x||0,node.y||0,duration);
    fg.zoom(1.75,duration);
  }
  function zoom(mult){
    const fg=fgRef.current; if(!fg) return;
    fg.zoom(Math.max(.15,Math.min(5,fg.zoom()*mult)),260);
  }
  function fit(){ fgRef.current?.zoomToFit(420,70); }
  function focusNode(n){
    const fg=fgRef.current; if(!fg||!n) return;
    const node=data.nodes.find(x=>x.id===n.id);
    if(node) frameNode(fg,node);
  }
  function clickNode(node){
    const now=Date.now();
    const last=lastClickRef.current;
    if(last.id===node.id && now-last.time<420){ lastClickRef.current={id:null,time:0}; onOpen(node.raw||node); }
    else { lastClickRef.current={id:node.id,time:now}; onSelect(node.raw||node); }
  }

  return <div ref={wrapRef} className="graph-wrap forcegraph-wrap">
    <ForceGraph2D
      ref={fgRef}
      graphData={data}
      backgroundColor="rgba(0,0,0,0)"
      nodeRelSize={1}
      nodeId="id"
      nodeVal={node=>node.size}
      nodeLabel={node=>displayNodeTitle(node.raw||node)}
      nodeCanvasObject={(node,ctx,globalScale)=>drawGraphNode(node,ctx,globalScale,selected)}
      nodePointerAreaPaint={(node,colour,ctx)=>{ctx.fillStyle=colour;ctx.beginPath();ctx.arc(node.x,node.y,Math.max(12,node.size||12),0,Math.PI*2);ctx.fill();}}
      linkSource="source"
      linkTarget="target"
      linkCurvature={edge=>edge.curveDistance||0}
      linkColor={edge=>edgeDirectionColour(edge,selected?.id)}
      linkWidth={edge=>edge.edge_type==='contains'?1.1:Math.max(1.4,Math.min(3.2,(edge.confidence||.55)*2.7))}
      linkLineDash={edge=>isInferred(edge)?[5,6]:null}
      linkDirectionalArrowLength={e=>e.edge_type==='contains'?0:10.5}
      linkDirectionalArrowRelPos={e=>e.edge_type==='contains'?1:.72}
      linkDirectionalArrowColor={e=>edgeDirectionColour(e,selected?.id)}
      linkCanvasObject={(edge,ctx,globalScale)=>drawGraphLink(edge,ctx,globalScale,selected)}
      linkCanvasObjectMode={()=>'after'}
      onNodeClick={clickNode}
      onNodeHover={node=>setHover(node?.raw||node||null)}
      onLinkHover={edge=>setHoverEdge(edge||null)}
      cooldownTicks={140}
      d3VelocityDecay={0.32}
      warmupTicks={80}
    />
    {hover&&<div className="node-tip forcegraph-tip"><span>{materialLabel(materialType(hover))}</span><strong>{displayNodeTitle(hover)}</strong><small>{truncate(hover.text||hover.url||'',180)}</small><small>Click to inspect · double-click to open/drill</small></div>}
    {hoverEdge&&<div className="node-tip forcegraph-tip edge-tip"><span>{edgeTooltip(hoverEdge,selected?.id)}</span><strong>{relationLabel(hoverEdge.edge_type)}</strong><small>{truncate(edgeTerm(hoverEdge)||edgeSummary(hoverEdge,selected?.id),180)}</small></div>}
    <Legend active={nodeTypes} relationshipTypes={relationshipTypes} relationshipFilters={relationshipFilters} availableEdgeTypes={availableEdgeTypes} onToggle={onToggleNodeType} onToggleRelationship={onToggleRelationship} />
    <div className="zoom"><button title="Zoom in" onClick={()=>zoom(1.18)}>＋</button><button title="Zoom out" onClick={()=>zoom(.86)}>−</button><button title="Fit graph" onClick={fit}>⤢</button><button title="Focus selected" onClick={()=>focusNode(detail||selected)}>◎</button></div>
  </div>;
}

function forceGraphData(graph,selected){
  const nodes=(graph.nodes||[]).map(node=>{
    return {...node,raw:node,id:node.id,role:relativeNodeRole(node,selected?.id,graph),badge:documentBadge(node),colour:nodeFill(node,graph),size:forceNodeSize(node,graph,selected),degree:node.degree||node.metadata?.weighted_degree||1};
  });
  const ids=new Set(nodes.map(n=>n.id));
  const parallelCounts=countParallelEdges(graph.edges||[]);
  const seen=new Map();
  const links=(graph.edges||[]).filter(edge=>ids.has(edge.from_node_id)&&ids.has(edge.to_node_id)).map((edge,i)=>{
    const key=parallelEdgeKey(edge);
    const parallelIndex=seen.get(key)||0;
    seen.set(key,parallelIndex+1);
    return {...edge,id:edge.id||`${edge.from_node_id}-${edge.to_node_id}-${edge.edge_type}-${parallelIndex}-${i}`,source:edge.from_node_id,target:edge.to_node_id,direction:edgeDirectionLabel(edge,selected?.id),curveDistance:parallelCurveDistance(parallelCounts,key,parallelIndex)};
  });
  return {nodes,links};
}
function forceNodeSize(node,graph,selected){
  if(node.id===selected?.id||node.id===graph?.centre_id) return 18;
  const base=r(node,graph);
  return Math.max(8,Math.min(18,base*.72));
}
function drawGraphNode(node,ctx,globalScale,selected){
  const raw=node.raw||node;
  const radius=node.size||10;
  const badge=node.badge;
  const role=node.role;
  const selectedNode=raw.id===selected?.id;
  ctx.save();
  ctx.beginPath();
  if(badge){ roundedRectPath(ctx,node.x-radius*1.45,node.y-radius*.85,radius*2.9,radius*1.7,5); }
  else if(role==='parent'){ ctx.rect(node.x-radius,node.y-radius,radius*2,radius*2); }
  else if(role==='child'){ ctx.moveTo(node.x,node.y-radius*1.2); ctx.lineTo(node.x+radius*1.15,node.y); ctx.lineTo(node.x,node.y+radius*1.2); ctx.lineTo(node.x-radius*1.15,node.y); ctx.closePath(); }
  else { ctx.arc(node.x,node.y,radius,0,Math.PI*2); }
  ctx.fillStyle=badge?.kind==='pdf'?'#b91c1c':badge?.kind==='spreadsheet'?'#047857':role==='parent'?'#fff1f2':node.colour||nodeFill(raw,{});
  ctx.fill();
  ctx.lineWidth=selectedNode?4:role==='parent'?3:2;
  ctx.setLineDash(role==='parent'||role==='child'?[4,3]:[]);
  ctx.strokeStyle=selectedNode?'#2457d6':role==='parent'?'#be123c':role==='child'?'#2563eb':'#ffffff';
  ctx.stroke();
  ctx.setLineDash([]);
  const label=forceGraphNodeLabel(node,selected,globalScale);
  if(label){ drawCanvasLabel(ctx,label,node.x,node.y+radius+11/globalScale,selectedNode?12:10,globalScale,selectedNode); }
  ctx.restore();
}
function forceGraphNodeLabel(node,selected,globalScale){
  const raw=node.raw||node;
  if(raw.id===selected?.id) return truncate(displayNodeTitle(raw),42);
  if(globalScale<.65) return '';
  if(node.badge) return truncate(displayNodeTitle(raw),30);
  if(node.role==='parent'||node.role==='child') return truncate(displayNodeTitle(raw),28);
  if((node.degree||0)>=4 || ['part','chapter','guidance_document','defined_term'].includes(raw.node_type)) return truncate(displayNodeTitle(raw),30);
  if(globalScale>1.05) return truncate(displayNodeTitle(raw),24);
  return '';
}
function drawGraphLink(edge,ctx,globalScale,selected){
  if(edge.edge_type==='contains') return;
  const sx=edge.source.x, sy=edge.source.y, tx=edge.target.x, ty=edge.target.y;
  if(!Number.isFinite(sx+sy+tx+ty)) return;
  const label='';
  if(label) drawCanvasLabel(ctx,label,(sx+tx)/2,(sy+ty)/2,8,globalScale,false);
}
function drawCanvasLabel(ctx,text,x,y,fontSize,globalScale,strong=false){
  const size=Math.max(8,fontSize/globalScale);
  ctx.font=`${strong?'800':'700'} ${size}px Inter, system-ui, sans-serif`;
  ctx.textAlign='center'; ctx.textBaseline='middle';
  const width=Math.min(220/globalScale,ctx.measureText(text).width+10/globalScale);
  const height=size+7/globalScale;
  ctx.fillStyle='rgba(255,255,255,.92)';
  roundedRectPath(ctx,x-width/2,y-height/2,width,height,5/globalScale); ctx.fill();
  ctx.strokeStyle='rgba(148,163,184,.45)'; ctx.lineWidth=1/globalScale; ctx.stroke();
  ctx.fillStyle='#172033'; ctx.fillText(text,x,y);
}
function roundedRectPath(ctx,x,y,w,h,r){
  ctx.beginPath(); ctx.moveTo(x+r,y); ctx.lineTo(x+w-r,y); ctx.quadraticCurveTo(x+w,y,x+w,y+r); ctx.lineTo(x+w,y+h-r); ctx.quadraticCurveTo(x+w,y+h,x+w-r,y+h); ctx.lineTo(x+r,y+h); ctx.quadraticCurveTo(x,y+h,x,y+h-r); ctx.lineTo(x,y+r); ctx.quadraticCurveTo(x,y,x+r,y); ctx.closePath();
}
function parallelCurveDistance(parallelCounts,key,index=0){
  const count=parallelCounts.get(key)||1;
  if(count<=1) return 0;
  const centre=(count-1)/2;
  return (index-centre)*0.18;
}



function Legend({active,relationshipTypes,relationshipFilters,availableEdgeTypes,onToggle,onToggleRelationship}){
  return <div className="legend" aria-label="Graph filters">
    <div className="legend-title">Node types</div>
    {MATERIAL_FILTERS.map(t=><button type="button" key={t} className={materialFilterOn(t,active)?'on':'off'} onClick={()=>onToggle(t)} title={`Toggle ${materialLabel(t)}`}><i style={{background:displayColour(t)}} />{materialLabel(t)}</button>)}
    <div className="legend-title">Edge types</div>
    {relationshipFilters.map(t=><button type="button" key={t} className={relationshipTypes?.has(t)?'on':'off'} onClick={()=>onToggleRelationship(t)} title={`Toggle ${relationLabel(t)}`}><i className={`line ${t==='contains'?'dash':''}`} style={{borderColor:edgeColour(t)}} />{relationLabel(t)}<em>{availableEdgeTypes?.[t]??''}</em></button>)}
  </div>;
}

function countParallelEdges(edges){
  const counts=new Map();
  for(const edge of edges||[]){
    const key=parallelEdgeKey(edge);
    counts.set(key,(counts.get(key)||0)+1);
  }
  return counts;
}
function parallelEdgeKey(edge){
  const a=edge.from_node_id||edge.source;
  const b=edge.to_node_id||edge.target;
  return `${a}→${b}`;
}

function Explore({node,edges,graph,onChoose}){
  return <div className="pane explore-pane"><Evidence node={node} edges={edges} graph={graph} onChoose={onChoose}/></div>;
}
function ContentNode({node,onChoose}){
  const kids=node.children||[];
  const number=node.metadata?.rule_number||node.metadata?.chapter_number||'';
  return <div className={`content-node ${node.node_type}`}>
    <button type="button" onClick={()=>onChoose(node)} aria-label={`Open ${node.title}`}>
      <span className="content-rail"><i>{kids.length?'▾':'›'}</i></span>
      <span className="content-body"><span className="content-meta"><b>child</b><em>{label(node.node_type)}</em>{number&&<em>{number}</em>}{kids.length>0&&<em>{kids.length} item{kids.length===1?'':'s'}</em>}</span><strong><NodeTitle node={node}/></strong>{node.text&&<small>{truncate(node.text,190)}</small>}</span>
      <span className="content-open">Open</span>
    </button>
    {kids.length>0&&<div className="content-children">{kids.map(k=><ContentNode key={k.id} node={k} onChoose={onChoose}/>)}</div>}
  </div>;
}

function Evidence({node,edges,graph,onChoose}){
  const byId=new Map(graph.nodes.map(n=>[n.id,n]));
  if(!node)return <section className="explore-layer evidence-layer"><p className="muted">Select a node.</p></section>;
  const analytical=edges.filter(e=>e.edge_type!=='contains');
  const groups=groupEdges(analytical);
  return <section className="explore-layer evidence-layer" aria-label="Connections">
    <div className="layer-head"><span>Connections</span><h3>Selected node</h3></div>
    <Collapsible title="Selected material" count={label(node.node_type)} open>
      <span className="kind">{label(node.node_type)}</span><h2><NodeTitle node={node}/></h2>{node.url&&<a className="source" href={node.url} target="_blank" rel="noopener noreferrer">Open source ↗</a>}
      <p className="text">{node.text?truncate(node.text,1300):emptyNodeMessage(node)}</p>
    </Collapsible>
    {groups.length
      ? groups.map(([edgeType,items],i)=><Collapsible key={edgeType} title={evidenceLabel(edgeType)} count={`${items.length} link${items.length===1?'':'s'}`} open={i<2}>
          <div className="edge-list">{items.slice(0,40).map(e=>{const other=byId.get(e.from_node_id===node.id?e.to_node_id:e.from_node_id);return <button key={e.id} className={`edge-direction-${edgeDirectionLabel(e,node.id)}`} onClick={()=>other&&onChoose(other)}><span><b className="edge-arrow">{edgeDirectionGlyph(e,node.id)}</b>{edgeSummary(e,node.id)}</span><strong><NodeTitle node={other}/></strong>{edgeContext(e,other)&&<small>{edgeContext(e,other)}</small>}{e.evidence_text&&<small>{truncate(e.evidence_text,160)}</small>}</button>})}</div>
          {items.length>40&&<p className="muted">Showing first 40 of {items.length} visible links. Increase the graph cap to load more.</p>}
        </Collapsible>)
      : <Collapsible title="Visible connections" count="0 links" open><p className="muted">No reference, definition, topic or obligation links are visible for this node under the current settings.</p></Collapsible>}
  </section>;
}
function NodeTitle({node}){
  const badge=documentBadge(node);
  const title=badge?displayNodeTitle(node).replace(new RegExp(`\\s*·\\s*${badge.label}$`),''):displayNodeTitle(node);
  return <>{badge&&<span className={`doc-chip ${badge.kind}`} aria-label={`${badge.label} document`}>{badge.label}</span>}{title}</>;
}
function Collapsible({title,count,open=false,children}){
  return <details className="collapse-card" open={open}><summary><span>{title}</span>{count&&<em>{count}</em>}</summary><div className="collapse-body">{children}</div></details>;
}
function groupEdges(edges){
  const priority=['has_permission','references','uses_defined_term','defines','shares_defined_term','has_topic','has_obligation_pattern','shares_obligation_pattern','has_structured_obligation','amends'];
  const buckets=new Map();
  for(const e of edges) buckets.set(e.edge_type,[...(buckets.get(e.edge_type)||[]),e]);
  return [...buckets.entries()].sort((a,b)=>{
    const ai=priority.indexOf(a[0]), bi=priority.indexOf(b[0]);
    return (ai<0?99:ai)-(bi<0?99:bi) || b[1].length-a[1].length || a[0].localeCompare(b[0]);
  });
}
function filterGraph(graph,nodeTypes,relationshipTypes,originFilter,selectedId,showInsurance=true){
  const keepNodes=(graph.nodes||[]).filter(n=>(nodeTypes.has(n.node_type)||n.id===selectedId) && (showInsurance || n.id===selectedId || !isInsuranceNode(n)));
  const keepIds=new Set(keepNodes.map(n=>n.id));
  return {...graph,nodes:keepNodes,edges:(graph.edges||[]).filter(e=>keepIds.has(e.from_node_id)&&keepIds.has(e.to_node_id)&&(!relationshipTypes?.size||relationshipTypes.has(e.edge_type))&&originMatches(e,originFilter))};
}

function graphBounds(nodes){
  if(!nodes.length) return {minX:0,minY:0,maxX:1200,maxY:820,width:1200,height:820};
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  for(const n of nodes){minX=Math.min(minX,n.x||0);minY=Math.min(minY,n.y||0);maxX=Math.max(maxX,n.x||0);maxY=Math.max(maxY,n.y||0);}
  const pad=80; minX-=pad; minY-=pad; maxX+=pad; maxY+=pad;
  return {minX,minY,maxX,maxY,width:Math.max(1,maxX-minX),height:Math.max(1,maxY-minY)};
}
function fitView(nodes){
  const b=graphBounds(nodes);
  const z=Math.max(.35,Math.min(2.2,Math.min(1200/b.width,820/b.height)*.92));
  return {x:600-((b.minX+b.maxX)/2)*z,y:410-((b.minY+b.maxY)/2)*z,z};
}
function layout(graph, centreId){
  const nodes=[...(graph.nodes||[])], edges=graph.edges||[]; if(!nodes.length)return{nodes,edges};
  if(['part','article'].includes(graph.level) && nodes.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y))) return {nodes:spreadNodes(nodes,graph),edges};
  const degree=new Map(nodes.map(n=>[n.id,0])); edges.forEach(e=>{degree.set(e.from_node_id,(degree.get(e.from_node_id)||0)+1); degree.set(e.to_node_id,(degree.get(e.to_node_id)||0)+1)});
  const centre=nodes.find(n=>n.id===centreId)||nodes[0], others=nodes.filter(n=>n.id!==centre.id).sort((a,b)=>(degree.get(b.id)||0)-(degree.get(a.id)||0));
  centre.x=600; centre.y=410; centre.degree=degree.get(centre.id)||1;
  others.forEach((n,i)=>{const ring=i<20?1:i<64?2:3; const idx=ring===1?i:ring===2?i-20:i-64; const count=ring===1?Math.min(20,others.length):ring===2?Math.min(44,Math.max(1,others.length-20)):Math.max(1,others.length-64); const a=(Math.PI*2*idx/count)+(ring*.21); const rad=ring===1?205:ring===2?335:455; n.x=600+Math.cos(a)*rad; n.y=410+Math.sin(a)*rad*.76; n.degree=degree.get(n.id)||1;});
  return{nodes:spreadNodes([centre,...others],graph),edges};
}
function spreadNodes(input,graph){
  const nodes=input.map(n=>({...n}));
  const minDist=graph?.level==='article'?18:graph?.level==='part'?34:22;
  const iterations=graph?.level==='article'?18:10;
  for(let k=0;k<iterations;k++){
    for(let i=0;i<nodes.length;i++) for(let j=i+1;j<nodes.length;j++){
      const a=nodes[i], b=nodes[j]; let dx=b.x-a.x, dy=b.y-a.y; let d=Math.hypot(dx,dy)||0.01;
      const need=minDist+(r(a,graph)+r(b,graph))*0.45;
      if(d<need){const push=(need-d)/2; dx/=d; dy/=d; a.x-=dx*push; a.y-=dy*push; b.x+=dx*push; b.y+=dy*push;}
    }
  }
  for(const n of nodes){n.x=Math.max(35,Math.min(1165,n.x)); n.y=Math.max(35,Math.min(785,n.y));}
  return nodes;
}
function r(n,graph){
  if(n.visual?.radius) return n.visual.radius;
  if(graph?.level==='part') return Math.min(34,8+Math.sqrt(Math.max(1,n.degree||n.metadata?.weighted_degree||1))*1.15);
  return Math.min(25,(n.node_type==='part'?14:n.node_type==='topic'?13:n.node_type==='defined_term'?11:9)+Math.sqrt(n.degree||1));
}
function showNodeLabel(n,view,graph,selected){
  if(selected?.id===n.id) return true;
  if(view.z<0.72) return false;
  if(graph?.level==='article') return view.z>1.08 && (n.degree||0)>6;
  if(graph?.level==='part') return view.z>0.82 || (n.degree||0)>80;
  return view.z>0.7;
}
function labelSize(view,graph){return graph?.level==='article'?Math.max(8,11/view.z):Math.max(9,12/view.z)}
function labelOffset(view){return 16/view.z}
function labelChars(n,view,graph,selected){
  if(selected?.id===n.id) return 54;
  if(graph?.level==='article') return view.z>1.35?34:22;
  return view.z>1.2?42:26;
}
function nodeFill(n,graph){
  if(n.visual?.colour) return n.visual.colour;
  if(graph?.level==='part' || graph?.level==='article') return CLUSTER_COLOURS[(n.metadata?.semantic_cluster??0)%CLUSTER_COLOURS.length];
  return MATERIAL_COLOURS[materialType(n)]||'#64748b';
}
function emptyNodeMessage(node){
  if(['part','chapter','guidance_document','guidance_section','rulebook'].includes(node?.node_type)) return 'This is a heading or container node. The substantive legal text is held in the child provision nodes shown in the left-hand contents panel.';
  if(node?.metadata?.placeholder) return 'This is a placeholder reference node. Open the source link for the external definition or referenced material.';
  return 'No body text for this node.';
}
function edgeColour(v){return EDGE_COLOURS[v]||'#94a3b8'}
function edgeDirectionColour(e,currentId){
  const dir=edgeDirectionLabel(e,currentId);
  if(dir==='incoming') return '#be123c';
  if(dir==='outgoing') return '#2563eb';
  return e.visual?.colour||edgeColour(e.edge_type);
}
function relationLabel(v){return RELATION_LABELS[v]||String(v||'').replaceAll('_',' ')}
function evidenceLabel(v){return EVIDENCE_LABELS[v]||relationLabel(v)}
function isInferred(e){return !EXPLICIT.has(e.source_method)}
function originMatches(e,originFilter){
  if(originFilter==='explicit') return !isInferred(e);
  if(originFilter==='inferred') return isInferred(e);
  return true;
}
function provenanceLabel(method){
  return ({
    rollup_child_edge:'contained in sub-provision',
    rollup_resolved_part_reference:'contained in sub-provision',
    derived_term_overlap:'shared defined term',
    derived_obligation_overlap:'similar obligation wording',
    keyword_topic:'topic match',
    regex_obligation:'obligation wording',
    structured_obligation_parser:'extracted obligation',
    regex_reference:'detected reference',
    regex_named_reference:'named reference',
    regex_article_reference:'article reference',
    resolved_part_reference:'resolved Part reference',
    llm_extracted_reference:'detected reference',
    html_link:'source link',
    html_anchor_resolved:'source link',
    html_glossary_link:'source glossary link',
    glossary_source:'glossary definition',
    crr_terms_source:'CRR term definition',
    legal_instrument_listing:'legal instrument',
    fca_waivers_list:'FCA waiver/permission list',
    site_structure:'document structure',
    inline_part_definition:'definition in rule text',
  }[method]||String(method||'').replaceAll('_',' '));
}
function edgeSummary(e,currentId){
  const confidence=`${Math.round((e.confidence||0)*100)}%`;
  const direction=currentId?`${edgeDirectionLabel(e,currentId)} · `:'';
  return `${direction}${relationLabel(e.edge_type)} · ${provenanceLabel(e.source_method)} · ${confidence}`;
}
function edgeTerm(e){
  return e.metadata?.term_title || e.evidence_text || e.metadata?.reference || e.metadata?.target_title || '';
}
function edgeTooltip(e,currentId){
  const term=edgeTerm(e);
  const direction=currentId?`${edgeDirectionLabel(e,currentId)} `:'';
  return term ? `${direction}${relationLabel(e.edge_type)}: ${term}` : edgeSummary(e,currentId);
}
function edgeNodeTitle(node,e,current){
  return displayNodeTitle(node);
}
function edgeContext(e,node){
  const meta=e.metadata||{};
  if(e.source_method==='rollup_child_edge'){
    const child=meta.rolled_up_from_title||meta.target_title;
    const container=meta.container_title;
    return [child&&`sub-provision: ${child}`,container&&`contained in: ${container}`].filter(Boolean).join(' · ');
  }
  const part=node?.metadata?.part_title||node?.metadata?.document_title;
  if(part && !String(node?.title||'').startsWith(part)) return part;
  return '';
}
function availableRelationshipTypes(stats,graph){
  const seen=new Set([...Object.keys(stats?.edges_by_type||{}),...Object.keys(graph?.available_edge_types||{})]);
  return RELATIONSHIP_ORDER.filter(t=>seen.has(t) && ((stats?.edges_by_type?.[t]||0)>0 || (graph?.available_edge_types?.[t]||0)>0));
}
function relationshipCount(t,stats,graph){return graph?.available_edge_types?.[t] ?? stats?.edges_by_type?.[t] ?? 0}
function materialFilterOn(t,active){
  const groups={
    rule:['rule','chapter','part','rulebook'],
    definition:['defined_term','glossary','crr_terms_list'],
    supervisory_statement:['guidance_document','guidance_section','guidance_paragraph'],
    statement_of_policy:['guidance_document','guidance_section','guidance_paragraph'],
    legal_instrument:['legal_instrument'],
    permission:['permission'],
    external_reference:['external_reference','rule_reference'],
  };
  return (groups[t]||[t]).some(x=>active?.has(x));
}
function materialType(n){
  const type=typeof n==='string'?n:n?.node_type;
  const meta=(typeof n==='string'?{}:n?.metadata)||{};
  const url=(typeof n==='string'?'':n?.url||'').toLowerCase();
  const doc=(meta.document_type||'').toLowerCase();
  if(['rule','chapter','part','rulebook'].includes(type)) return 'rule';
  if(['defined_term','glossary','crr_terms_list'].includes(type)) return 'definition';
  if(type==='legal_instrument') return 'legal_instrument';
  if(type==='permission') return 'permission';
  if(type==='external_reference' || type==='rule_reference') return 'external_reference';
  if(['topic','topic_cluster','obligation_pattern','obligation_statement'].includes(type)) return type;
  if(['guidance_document','guidance_section','guidance_paragraph'].includes(type)){
    if(doc.includes('statement_of_policy') || url.includes('/statements-of-policy/')) return 'statement_of_policy';
    return 'supervisory_statement';
  }
  return type||'external_reference';
}
function materialLabel(v){return ({rule:'Rulebook part / rule',supervisory_statement:'Supervisory statement',statement_of_policy:'Statement of policy',definition:'Definition',permission:'Firm permission',external_reference:'External reference',legal_instrument:'Legal instrument',topic:'Topic assignment',topic_cluster:'Semantic topic cluster',obligation_pattern:'Obligation pattern',obligation_statement:'Structured obligation',analysis:'Topic / obligation marker'}[v]||String(v||'').replaceAll('_',' '))}
function displayColour(v){return MATERIAL_COLOURS[materialType(v)]||'#64748b'}
function label(v){return materialLabel(materialType(v))}
function truncate(s='',n=120){return s&&s.length>n?s.slice(0,n-1)+'…':s}

createRoot(document.getElementById('root')).render(<App/>);
