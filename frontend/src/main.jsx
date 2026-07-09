import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import ForceGraph2D from 'react-force-graph-2d';
import { forceCollide, forceX, forceY } from 'd3-force';
import { filterGraph, isInsuranceNode } from './graphFilters.js';
import { buildQualityQueues, filterQueueRows, summariseQueue } from './qualityWorkbench.js';
import { displayNodeTitle, documentBadge, relativeNodeRole, edgeDirectionGlyph, edgeDirectionLabel } from './graphPresentation.js';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || '/pra-rulebook-api';
const TYPES = ['contains','references','uses_defined_term','defines','shares_defined_term','has_obligation_pattern','has_structured_obligation','shares_obligation_pattern','amends','has_permission'];
const PROVISION_TYPES = ['rule','chapter','guidance_section','guidance_paragraph'];
const NODE_TYPES = [...PROVISION_TYPES,'part','rulebook','defined_term','glossary','crr_terms_list','guidance_document','obligation_pattern','obligation_statement','legal_instrument','permission','external_reference','rule_reference'];
const DEFAULT_TYPES = new Set(['contains','references']);
const REPRESENTATIONS = {
  combined: { label:'Combined', hint:'Legal structure plus rolled-up references, terms, obligations and permissions.', types:[...DEFAULT_TYPES], depth:1, explicitOnly:false },
  hierarchy: { label:'Legal hierarchy', hint:'Parts, articles, chapters, rules and paragraphs only.', types:['contains'], depth:2, explicitOnly:false },
  references: { label:'Cross-references', hint:'Explicit and detected cross-reference/amendment links, with child context so Article-level headings expose paragraph-level references.', types:['contains','references','amends'], depth:2, explicitOnly:false },
  definitions: { label:'Definitions', hint:'Definitions, glossary/CRR term usage, and provisions sharing defined terms.', types:['uses_defined_term','defines','shares_defined_term'], depth:2, explicitOnly:false },
  obligations: { label:'Obligations', hint:'Detected obligation statements, obligation patterns, and provisions with similar obligation patterns.', types:['has_obligation_pattern','has_structured_obligation','shares_obligation_pattern'], depth:1, explicitOnly:false },
};
const EXPLICIT = new Set(['site_structure','html_link','html_anchor_resolved','html_glossary_link','glossary_source','crr_terms_source','legal_instrument_listing','regex_reference','regex_named_reference','llm_extracted_reference','resolved_part_reference','fca_waivers_list']);
const RELATION_LABELS = { contains:'contains / child', references:'Cross-references', uses_defined_term:'Definitions used', defines:'Definitions provided', shares_defined_term:'Shared defined terms', has_obligation_pattern:'Obligation themes', shares_obligation_pattern:'Similar obligations', has_structured_obligation:'Extracted obligations', amends:'Amendments', has_permission:'Firms with permissions', USES_TEMPLATE:'Uses template', USES_INSTRUCTIONS:'Uses instructions', EVIDENCED_BY:'Evidenced by', LEGAL_BASIS:'Legal basis', APPLIES_TO:'Applies to', HAS_SCOPE_RULE:'Scope rule', MAY_BE_AFFECTED_BY_PERMISSION:'Affected by permission', REFERENCES_RULE:'References rule', REFERENCES_SOURCE:'References source', REFERENCES_EXTERNAL:'References external', REFERENCES_RETURN:'References return', REFERENCES_TEMPLATE:'References template', SUMMARISES_DATAPOINTS:'Summarises datapoints', HAS_DATAPOINT:'Has datapoint', REPORTS_CONCEPT:'Reports concept' };
const EVIDENCE_LABELS = { references:'Cross-references', uses_defined_term:'Definitions used by this provision', defines:'Definitions provided here', shares_defined_term:'Provisions sharing defined terms', has_obligation_pattern:'Obligation themes found here', shares_obligation_pattern:'Provisions with similar obligations', has_structured_obligation:'Extracted obligation statements', amends:'Legal instruments amending this material', has_permission:'Firms with active permissions' };
const ORIGIN_FILTERS = { all:'All links', explicit:'Direct links', inferred:'Inferred / derived links' };
const EDGE_COLOURS = { contains:'#94a3b8', references:'#2563eb', uses_defined_term:'#d97706', defines:'#ca8a04', shares_defined_term:'#0f766e', has_obligation_pattern:'#db2777', shares_obligation_pattern:'#ea580c', has_structured_obligation:'#be123c', amends:'#dc2626', has_permission:'#8b5cf6', USES_TEMPLATE:'#2563eb', USES_INSTRUCTIONS:'#0f766e', EVIDENCED_BY:'#7c3aed', LEGAL_BASIS:'#dc2626', APPLIES_TO:'#0891b2', HAS_SCOPE_RULE:'#0d9488', MAY_BE_AFFECTED_BY_PERMISSION:'#8b5cf6', REFERENCES_RULE:'#be123c', REFERENCES_SOURCE:'#9333ea', REFERENCES_EXTERNAL:'#64748b', REFERENCES_RETURN:'#ea580c', REFERENCES_TEMPLATE:'#4f46e5', SUMMARISES_DATAPOINTS:'#475569', HAS_DATAPOINT:'#94a3b8', REPORTS_CONCEPT:'#0f766e' };
const MATERIAL_COLOURS = { rule:'#2563eb', supervisory_statement:'#16a34a', statement_of_policy:'#0f766e', definition:'#b45309', permission:'#8b5cf6', external_reference:'#64748b', legal_instrument:'#b91c1c', obligation_pattern:'#db2777', obligation_statement:'#be123c', analysis:'#9333ea', rulebook:'#6d28d9', reporting_return:'#2457d6', reporting_template:'#0f766e', reporting_instruction:'#d97706', reporting_source:'#7c3aed', reporting_xbrl_source:'#6d28d9', reporting_datapoint:'#64748b', reporting_provision:'#be123c', reporting_concept:'#0891b2' };
const CLUSTER_COLOURS = ['#4f7cff','#d28b24','#58a978','#d35cff','#cc5c5c','#35b6b4','#d7ff64','#a78bfa','#fb7185','#60a5fa','#f59e0b','#34d399'];
const MATERIAL_FILTERS = ['rule','supervisory_statement','statement_of_policy','definition','permission','legal_instrument','external_reference'];
const RELATIONSHIP_ORDER = TYPES;
const REPORTING_NODE_TYPES = ['DataItem','ReportingObligation','Template','InstructionSet','SourceDocument','Provision','ExternalReference','LegalInstrument','PolicyStatement','TemplateSet','DataPointGroup','DataPoint','TemplateRow','TemplateColumn','Concept','ScopeRule','FirmType','Permission','ValidationRule'];
const REPORTING_EDGE_TYPES = ['USES_TEMPLATE','USES_INSTRUCTIONS','EVIDENCED_BY','LEGAL_BASIS','APPLIES_TO','HAS_SCOPE_RULE','MAY_BE_AFFECTED_BY_PERMISSION','REFERENCES_RULE','REFERENCES_SOURCE','REFERENCES_EXTERNAL','REFERENCES_RETURN','REFERENCES_TEMPLATE','SUMMARISES_DATAPOINTS','HAS_DATAPOINT','REPORTS_CONCEPT'];
const REPORTING_DEFAULT_EDGE_TYPES = new Set(['USES_TEMPLATE','USES_INSTRUCTIONS','EVIDENCED_BY','LEGAL_BASIS','APPLIES_TO','HAS_SCOPE_RULE','MAY_BE_AFFECTED_BY_PERMISSION','SUMMARISES_DATAPOINTS']);

async function fetchJson(url,options){
  const res=await fetch(url,options);
  if(!res.ok) throw new Error(await responseErrorText(res));
  return res.json();
}

async function responseErrorText(res){
  const text=await res.text();
  try{
    const payload=JSON.parse(text);
    if(payload?.detail) return Array.isArray(payload.detail)?payload.detail.map(d=>d.msg||JSON.stringify(d)).join('; '):String(payload.detail);
  }catch{}
  return text || `Request failed with status ${res.status}`;
}

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
  const [feedbackNode,setFeedbackNode]=useState(null);
  const [feedbackText,setFeedbackText]=useState('');
  const [feedbackSaving,setFeedbackSaving]=useState(false);

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
  async function submitNodeFeedback(e){
    e?.preventDefault();
    if(!feedbackNode || !feedbackText.trim()) return;
    setFeedbackSaving(true); setError('');
    try{
      const res=await fetch(API_BASE+'/feedback/node',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({node:feedbackNode,feedback:feedbackText.trim(),page_url:window.location.href})});
      if(!res.ok) throw new Error(await responseErrorText(res));
      setFeedbackNode(null); setFeedbackText('');
    }catch(err){ setError(err.message||String(err)); }
    finally{ setFeedbackSaving(false); }
  }

  function toggleNodeType(t){
    const next=new Set(nodeTypes);
    const groups={
      rule:['rule','chapter','part','rulebook'],
      definition:['defined_term','glossary','crr_terms_list'],
      supervisory_statement:['guidance_document','guidance_section','guidance_paragraph'],
      statement_of_policy:['guidance_document','guidance_section','guidance_paragraph'],
      analysis:['obligation_pattern','obligation_statement'],
      permission:['permission','Permission'],
      legal_instrument:['legal_instrument','LegalInstrument'],
      external_reference:['external_reference','rule_reference','ExternalReference'],
      reporting_return:['DataItem'],
      reporting_template:['Template','TemplateSet'],
      reporting_instruction:['InstructionSet'],
      reporting_source:['SourceDocument'],
      reporting_xbrl_source:['SourceDocument','TemplateSet'],
      reporting_datapoint:['DataPointGroup','DataPoint','TemplateRow','TemplateColumn'],
      reporting_provision:['Provision'],
      reporting_concept:['Concept','ScopeRule','FirmType','Metric','CalculationRule','ValidationRule'],
    };
    const group=groups[t]||[t];
    const allOn=group.every(x=>next.has(x));
    group.forEach(x=>allOn?next.delete(x):next.add(x));
    setNodeTypes(next);
  }

  return <div className={`${graphExpanded?'shell graph-expanded':'shell'} ${panelOpen?'panel-open':'panel-closed'} ${view==='quality'?'quality-view':''} ${view==='reporting'?'reporting-view-mode':''}`}>
    <header className="topbar">
      <a className="home" href="/">‹</a>
      <form className="command" onSubmit={search}>
        <span>⌕</span><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search, or leave blank for all Parts" autoFocus/><button>{busy?'…':'Search'}</button>
      </form>
      <div className="top-actions">
        <button className={view==='graph'?'mode on':'mode'} onClick={()=>setView('graph')}>Graph</button>
        <button className={view==='reporting'?'mode on':'mode'} onClick={()=>{setView('reporting');setPanelOpen(false);}}>Reporting</button>
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
      {view==='quality'?<ValidationDashboard data={validation} busy={busy}/>:view==='reporting'?<ReportingGraphView onFeedback={n=>{setFeedbackNode(n);setFeedbackText('');}}/>:<>
        <div className="canvas-meta"><strong>{selected?.title||'Select a node'}</strong><span>{activeRep.label} · {visibleGraph.nodes.length} shown · {visibleGraph.edges.length} visible links · {Object.values(graph.available_edge_types||{}).reduce((a,b)=>a+b,0)} direct links available</span><button className="expand-graph" onClick={()=>setGraphExpanded(v=>!v)}>{graphExpanded?'Collapse graph':'Expand graph'}</button></div>
        <Graph graph={visibleGraph} selected={selected} detail={detail} nodeTypes={nodeTypes} relationshipTypes={types} relationshipFilters={relationshipFilters} availableEdgeTypes={graph.available_edge_types||{}} onToggleNodeType={toggleNodeType} onToggleRelationship={toggleType} onSelect={n=>{setDetail(n);setPanelOpen(true);}} onOpen={n=>choose(n,{drill:true})} onFeedback={n=>{setFeedbackNode(n);setFeedbackText('');}}/>
      </>}
    </main>

    <aside className={panelOpen?'inspector open':'inspector'}>
      <Explore node={detail} edges={selectedEdges} graph={graph} onChoose={choose}/>
    </aside>
    {feedbackNode&&<NodeFeedbackModal node={feedbackNode} text={feedbackText} setText={setFeedbackText} saving={feedbackSaving} onClose={()=>setFeedbackNode(null)} onSubmit={submitNodeFeedback}/>}  
  </div>;
}

function NodeFeedbackModal({node,text,setText,saving,onClose,onSubmit}){
  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Provide feedback on this node">
    <form className="node-feedback-modal" onSubmit={onSubmit}>
      <div className="modal-head"><div><span className="eyebrow">Node feedback</span><h3>Provide feedback on this node</h3></div><button type="button" onClick={onClose} aria-label="Close">×</button></div>
      <div className="feedback-node-summary"><span>{label(node.node_type)}</span><strong>{displayNodeTitle(node)}</strong>{node.url&&<a href={node.url} target="_blank" rel="noopener noreferrer">Open source</a>}</div>
      <label className="feedback-editor">What should Declan fix or investigate?<textarea value={text} onChange={e=>setText(e.target.value)} placeholder="Example: this node should link to SS3/18, but the reference is missing." autoFocus/></label>
      <div className="modal-actions"><button type="button" onClick={onClose}>Cancel</button><button type="submit" disabled={saving||!text.trim()}>{saving?'Saving…':'Add to feedback queue'}</button></div>
    </form>
  </div>;
}

function ReportingGraphView({onFeedback}){
  const [query,setQuery]=useState('');
  const [submitted,setSubmitted]=useState('');
  const [selectedReturn,setSelectedReturn]=useState('');
  const [includeDatapoints,setIncludeDatapoints]=useState(false);
  const [edgeTypes,setEdgeTypes]=useState(new Set(REPORTING_DEFAULT_EDGE_TYPES));
  const [nodeTypes,setNodeTypes]=useState(new Set(REPORTING_NODE_TYPES.filter(t=>t!=='DataPoint'&&t!=='TemplateRow'&&t!=='TemplateColumn')));
  const [graph,setGraph]=useState({nodes:[],edges:[],available_edge_types:{}});
  const [detail,setDetail]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const activeGraph=useMemo(()=>filterGraph(graph,nodeTypes,edgeTypes,'all',detail?.id,true),[graph,nodeTypes,edgeTypes,detail?.id]);
  const selectedEdges=useMemo(()=>activeGraph.edges.filter(e=>detail&&(e.from_node_id===detail.id||e.to_node_id===detail.id)),[activeGraph,detail]);
  const reportingRoot=useMemo(()=>graph.nodes?.find(n=>n.node_type==='DataItem')||detail||null,[graph.nodes,detail?.id]);
  useEffect(()=>{ loadReportingGraph(selectedReturn?'':submitted, selectedReturn); },[includeDatapoints]);

  async function loadReportingGraph(q=submitted, returnCode=''){
    setBusy(true); setError('');
    try{
      const p=new URLSearchParams({limit:'80',child_limit:includeDatapoints?'1400':'900',include_datapoints:String(includeDatapoints)});
      if(returnCode) p.set('selected_return',returnCode);
      else if(q.trim()) p.set('q',q.trim());
      const data=await fetchJson(API_BASE+`/reporting/graph/overview?${p}`);
      setGraph(data);
      setSubmitted(returnCode||q.trim());
      setSelectedReturn(returnCode);
      const first=returnCode ? (data.nodes?.find(n=>n.node_type==='DataItem')||data.nodes?.[0]||null) : null;
      setDetail(first);
    }catch(err){ setError(err.message||String(err)); }
    finally{ setBusy(false); }
  }
  function submit(e){ e?.preventDefault(); loadReportingGraph(query,''); }
  function returnCode(node){ return node?.metadata?.data_item_code || String(node?.title||node?.id||'').replace(/^data_item:/,''); }
  function inspectReportingNode(node){ setDetail(node); }
  function drillReportingNode(node){ if(node?.node_type==='DataItem') loadReportingGraph('', returnCode(node)); else setDetail(node); }
  function openReturn(node){ drillReportingNode(node); }
  function showAllReturns(){ setQuery(''); loadReportingGraph('', ''); }
  function toggleEdge(t){ const next=new Set(edgeTypes); next.has(t)?next.delete(t):next.add(t); setEdgeTypes(next); }
  function toggleNode(t){ const next=new Set(nodeTypes); const group=reportingNodeTypeGroup(t); const allOn=group.every(x=>next.has(x)); group.forEach(x=>allOn?next.delete(x):next.add(x)); setNodeTypes(next); }
  const roots=graph.nodes.filter(n=>n.node_type==='DataItem');
  const visibleEdgeTypes=REPORTING_EDGE_TYPES.filter(t=>(graph.available_edge_types?.[t]||0)>0 || edgeTypes.has(t));
  const visibleMaterialFilters=reportingMaterialFilters(graph);

  return <section className="reporting-view">
    <div className="reporting-toolbar">
      <div><span className="eyebrow">Reporting estate</span><h2>{selectedReturn?'Return drilldown':'Returns overview'}</h2></div>
      <form onSubmit={submit}><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Filter returns, e.g. COR011, PRA110, liquidity…"/><button>{busy?'Loading…':'Load'}</button></form>
      <label className="check"><input type="checkbox" checked={includeDatapoints} onChange={e=>setIncludeDatapoints(e.target.checked)}/> Summarise datapoints</label>
      {selectedReturn&&<button className="ghost" onClick={showAllReturns}>Show all returns</button>}
    </div>
    {error&&<div className="error">{error}</div>}
    <div className="reporting-layout">
      <ReportingRail roots={roots} selectedReturn={selectedReturn} detail={detail} graph={activeGraph} onOpen={inspectReportingNode} onDrill={drillReportingNode} onBackToOverview={showAllReturns}/>
      <main className="reporting-canvas">
        <div className="canvas-meta reporting-meta"><strong>{selectedReturn?`Reporting graph: ${selectedReturn}`:submitted?`Returns matching: ${submitted}`:'Reporting returns overview'}</strong><span>{activeGraph.nodes.length} shown · {activeGraph.edges.length} visible links · {selectedReturn?(includeDatapoints?'datapoints summarised':'datapoints hidden'):'click to inspect · double-click to drill'}</span></div>
        <Graph graph={activeGraph} selected={reportingRoot} detail={detail} nodeTypes={nodeTypes} relationshipTypes={edgeTypes} relationshipFilters={visibleEdgeTypes} materialFilters={visibleMaterialFilters} availableEdgeTypes={graph.available_edge_types||{}} onToggleNodeType={toggleNode} onToggleRelationship={toggleEdge} onSelect={inspectReportingNode} onOpen={drillReportingNode} onFeedback={onFeedback}/>
      </main>
      <aside className="reporting-inspector"><ReportingInspector node={detail} edges={selectedEdges} graph={activeGraph}/></aside>
    </div>
  </section>;
}

function ReportingRail({roots,selectedReturn,detail,graph,onOpen,onDrill,onBackToOverview}){
  if(!selectedReturn) return <aside className="reporting-rail">
    <h3>Returns</h3>
    <div className="reporting-return-groups">{groupReportingReturns(roots).map(group=><section className="reporting-return-group" key={group.key}>
      <h4>{group.label}<span>{group.returns.length}</span></h4>
      <div className="reporting-return-list">{group.returns.map(n=><button key={n.id} className={detail?.id===n.id?'active':''} onClick={()=>onDrill(n)}><strong>{n.title}</strong><small>{reportingReturnSummary(n)}</small></button>)}</div>
    </section>)}</div>
  </aside>;
  const root=graph.nodes.find(n=>n.node_type==='DataItem')||roots[0];
  const railGroups=reportingRailGroups(detail||root,graph);
  const sampleDatapoints=reportingSampleDatapoints(detail,graph);
  return <aside className="reporting-rail">
    <button type="button" className="reporting-nav-back" onClick={onBackToOverview}>‹ Back to returns overview</button>
    {root&&detail?.id!==root.id&&<button type="button" className="reporting-nav-back secondary" onClick={()=>onOpen(root)}>↑ Back to return</button>}
    <h3>{selectedReturn}</h3>
    <div className="reporting-return-list">{root&&<button className={detail?.id===root.id?'active':''} onClick={()=>onOpen(root)}><strong>{displayNodeTitle(root)}</strong><small>Return root</small></button>}</div>
    {railGroups.map(group=><section className="reporting-return-group" key={group.key}>
      <h4>{group.label}<span>{group.items.length}</span></h4>
      <div className="reporting-return-list">{group.items.map(n=><button key={n.id} className={detail?.id===n.id?'active':''} onClick={()=>onOpen(n)}><strong>{displayNodeTitle(n)}</strong><small>{reportingNodeSummary(n)}</small></button>)}</div>
    </section>)}
    {sampleDatapoints.length>0&&<>
      <h3>Sample datapoints</h3>
      <div className="reporting-sample-list">{sampleDatapoints.map((text,i)=><button type="button" key={`${text}-${i}`} onClick={()=>{}}><strong>{text.split('|')[0]?.trim()||`Row ${i+1}`}</strong><small>{text.includes('|')?text.split('|').slice(1).join('|').trim():text}</small></button>)}</div>
    </>}
  </aside>;
}

function groupReportingReturns(roots){
  const groups=new Map();
  for(const node of roots||[]){
    const estate=reportingEstateForReturn(node);
    if(!groups.has(estate.key)) groups.set(estate.key,{...estate,returns:[]});
    groups.get(estate.key).returns.push(node);
  }
  return [...groups.values()]
    .sort((a,b)=>a.order-b.order||a.label.localeCompare(b.label))
    .map(g=>({...g,returns:g.returns.sort(compareReportingReturns)}));
}
function reportingReturnSummary(node){
  const meta=node?.metadata||{};
  const bits=[];
  appendCountSummary(bits,meta.template_count,'template');
  appendCountSummary(bits,meta.source_document_count,'source');
  if(meta.submission_system) bits.push(String(meta.submission_system));
  if(meta.reporting_domain) bits.push(String(meta.reporting_domain));
  return bits.filter(Boolean).slice(0,4).join(' · ') || 'Open return drilldown';
}

function appendCountSummary(bits,value,label){
  if(value===undefined || value===null || value==='') return;
  const count=Number(value);
  if(!Number.isFinite(count)) return;
  bits.push(`${fmt(count)} ${label}${count===1?'':'s'}`);
}
function reportingEstateForReturn(node){
  const code=reportingReturnCode(node);
  if(/^COR\d+/.test(code)||/^COREP(?:-|$)/.test(code)) return {key:'corep',label:'COREP returns',order:10};
  if(/^PRA\d+/.test(code)) return {key:'pra',label:'PRA returns',order:20};
  if(/^FSA\d+/.test(code)) return {key:'fsa',label:'FSA returns',order:30};
  if(/^FINREP(?:-|$)/.test(code)) return {key:'finrep',label:'FINREP returns',order:40};
  if(/^LVR\d+/.test(code)) return {key:'lvr',label:'Leverage ratio returns',order:50};
  if(/^MLAR(?:-|$)/.test(code)) return {key:'mlar',label:'MLAR returns',order:60};
  if(/^RFB\d+/.test(code)) return {key:'rfb',label:'Ring-fencing returns',order:70};
  if(/^REP\d+/.test(code)) return {key:'rep',label:'REP returns',order:80};
  if(/^IDY\d+/.test(code)) return {key:'idy',label:'Identity returns',order:90};
  if(/PILLAR3|DISCLOSURE/.test(code)) return {key:'pillar3',label:'Pillar 3 disclosure',order:100};
  if(/TAXONOMY|DPM|XBRL/.test(code)) return {key:'taxonomy',label:'Taxonomy and XBRL',order:110};
  return {key:'other',label:'Other reporting',order:999};
}
function reportingReturnCode(node){
  return String(node?.metadata?.data_item_code||node?.title||node?.id||'').replace(/^data_item:/,'').toUpperCase();
}
function compareReportingReturns(a,b){
  const ac=reportingReturnCode(a), bc=reportingReturnCode(b);
  return compareReturnCode(ac,bc)||displayNodeTitle(a).localeCompare(displayNodeTitle(b));
}
function compareReturnCode(a,b){
  const ap=returnCodeParts(a), bp=returnCodeParts(b);
  const prefix=ap.prefix.localeCompare(bp.prefix);
  if(prefix) return prefix;
  if(ap.number!==bp.number) return ap.number-bp.number;
  return ap.suffix.localeCompare(bp.suffix,undefined,{numeric:true,sensitivity:'base'});
}
function returnCodeParts(code){
  const m=String(code||'').match(/^([A-Z]+)(\d+)?(.*)$/);
  return {prefix:m?.[1]||String(code||''),number:m?.[2]?Number(m[2]):Number.MAX_SAFE_INTEGER,suffix:m?.[3]||''};
}

function reportingNeighbours(node,graph){
  if(!node) return [];
  const byId=new Map((graph.nodes||[]).map(n=>[n.id,n]));
  const seen=new Set();
  const rows=[];
  for(const edge of graph.edges||[]){
    if(edge.from_node_id!==node.id && edge.to_node_id!==node.id) continue;
    const other=byId.get(edge.from_node_id===node.id?edge.to_node_id:edge.from_node_id);
    if(other && !seen.has(other.id)){ seen.add(other.id); rows.push(other); }
  }
  return rows.sort((a,b)=>materialLabel(materialType(a)).localeCompare(materialLabel(materialType(b)))||displayNodeTitle(a).localeCompare(displayNodeTitle(b)));
}

function reportingRailGroups(node,graph){
  const candidates=node?.node_type==='DataItem'
    ? (graph.nodes||[]).filter(n=>n.id!==node.id)
    : reportingNeighbours(node,graph);
  const sections=[
    {key:'templates',label:'Templates',types:['Template','TemplateSet']},
    {key:'instructions',label:'Instructions',types:['InstructionSet']},
    {key:'sources',label:'Sources',types:['SourceDocument','ExternalReference']},
    {key:'rules',label:'Rules and legal basis',types:['Provision','LegalInstrument','PolicyStatement','Permission']},
    {key:'concepts',label:'Concepts and scope',types:['Concept','ScopeRule','FirmType','Metric','CalculationRule','ValidationRule']},
    {key:'datapoints',label:'Datapoints',types:['DataPointGroup','DataPoint','TemplateRow','TemplateColumn']},
  ];
  const seen=new Set();
  return sections.map(section=>({
    ...section,
    items:candidates.filter(n=>{
      if(!section.types.includes(n.node_type) || seen.has(n.id)) return false;
      seen.add(n.id);
      return true;
    }).sort(compareReportingRailNodes),
  })).filter(section=>section.items.length>0);
}

function compareReportingRailNodes(a,b){
  return materialLabel(materialType(a)).localeCompare(materialLabel(materialType(b)))||displayNodeTitle(a).localeCompare(displayNodeTitle(b),undefined,{numeric:true,sensitivity:'base'});
}

function reportingNodeSummary(node){
  const meta=node?.metadata||{};
  const bits=[materialLabel(materialType(node))];
  appendCountSummary(bits,meta.datapoint_count,'datapoint');
  appendCountSummary(bits,meta.source_document_count,'source');
  if(meta.file_type) bits.push(String(meta.file_type).toUpperCase());
  if(meta.reporting_role) bits.push(String(meta.reporting_role));
  return bits.filter(Boolean).slice(0,4).join(' · ');
}

function reportingSampleDatapoints(node,graph){
  if(!node) return [];
  const groups=node.node_type==='DataPointGroup' ? [node] : reportingNeighbours(node,graph).filter(n=>n.node_type==='DataPointGroup');
  return groups.flatMap(g=>g.metadata?.sample_datapoints||g.metadata?.sample_labels||[]).slice(0,18);
}

function reportingNodeTypeGroup(t){
  return ({
    reporting_return:['DataItem'],
    reporting_template:['Template','TemplateSet'],
    reporting_instruction:['InstructionSet'],
    reporting_source:['SourceDocument'],
    reporting_xbrl_source:['SourceDocument','TemplateSet'],
    reporting_datapoint:['DataPointGroup','DataPoint','TemplateRow','TemplateColumn'],
    reporting_provision:['Provision'],
    reporting_concept:['Concept','ScopeRule','FirmType','Metric','CalculationRule','ValidationRule'],
    legal_instrument:['LegalInstrument'],
    permission:['Permission'],
    external_reference:['ExternalReference'],
  }[t]||[t]);
}

function reportingMaterialFilters(graph){
  const order=['reporting_return','reporting_template','reporting_instruction','reporting_xbrl_source','reporting_source','reporting_datapoint','reporting_provision','reporting_concept','legal_instrument','permission','external_reference'];
  const present=new Set((graph.nodes||[]).map(n=>materialType(n)));
  return order.filter(t=>present.has(t));
}

function ReportingInspector({node,edges,graph}){
  if(!node) return <div className="pane"><p className="muted">Select a reporting node.</p></div>;
  const neighbours=new Map(graph.nodes.map(n=>[n.id,n]));
  const grouped=groupEdges(edges);
  return <div className="pane explore-pane reporting-detail-pane">
    <span className="kind">{materialLabel(materialType(node))}</span>
    <h2>{displayNodeTitle(node)}</h2>
    {node.text&&<p className="text">{truncate(node.text,1200)}</p>}
    <ReportingMetadata node={node} edges={edges} graph={graph}/>
    {grouped.map(([type,rows],i)=><Collapsible key={type} title={relationLabel(type)} count={`${rows.length} links`} open={i<2}><div className="edge-list">{rows.slice(0,60).map(e=>{const other=neighbours.get(e.from_node_id===node.id?e.to_node_id:e.from_node_id);return <button key={e.id} type="button"><span>{edgeDirectionGlyph(e,node.id)} {edgeDirectionLabel(e,node.id)}</span><strong>{displayNodeTitle(other||{})}</strong>{e.evidence_text&&<small>{truncate(e.evidence_text,160)}</small>}</button>})}</div></Collapsible>)}
  </div>;
}

function ReportingMetadata({node,edges,graph}){
  const rows=reportingMetadataRows(node);
  const links=reportingUsefulLinks(node,edges,graph);
  return <>
    <Collapsible title="Useful links" count={links.length?`${links.length} items`:'no URL'} open>
      {links.length?<div className="source-link-list">{links.slice(0,14).map(link=>link.url?<a key={`${link.url}-${link.label}`} href={link.url} target="_blank" rel="noopener noreferrer"><span>{link.kind}</span><strong>{link.label}</strong><small>{compactUrl(link.url)}</small><em>Open source ↗</em></a>:<div className="source-link-item" key={`${link.kind}-${link.label}`}><span>{link.kind}</span><strong>{link.label}</strong>{link.detail&&<small>{link.detail}</small>}</div>)}</div>:<p className="muted">No source URL is attached to this node in the current graph. Related templates and datapoint summaries are shown in the left rail and link sections below.</p>}
    </Collapsible>
    <Collapsible title="Metadata" count={`${rows.length} fields`} open={rows.length<=6}>
      <dl className="metadata-list">{rows.map(row=><div key={row.key}><dt>{row.label}</dt><dd title={row.raw}>{row.value}</dd></div>)}</dl>
    </Collapsible>
  </>;
}

function reportingMetadataRows(node){
  const meta=node?.metadata||{};
  const fields=[
    ['data_item_code','Data item code'],
    ['reporting_domain','Reporting domain'],
    ['reporting_role','Reporting role'],
    ['submission_system','Submission system'],
    ['file_type','File type'],
    ['source_document_count','Source documents'],
    ['source_table','Source table'],
    ['source_pk','Source record key'],
    ['checksum_sha256','File checksum'],
    ['datapoint_count','Datapoints summarised'],
    ['template_count','Templates'],
    ['sample_labels','Sample datapoints'],
    ['title','Source title'],
  ];
  const used=new Set();
  const rows=[];
  for(const [key,label] of fields){
    if(meta[key]===undefined || meta[key]===null || meta[key]==='') continue;
    used.add(key);
    rows.push({key,label,value:formatReportingMetadataValue(key,meta[key]),raw:rawMetadataValue(meta[key])});
  }
  for(const [key,value] of Object.entries(meta)){
    if(used.has(key) || value===undefined || value===null || value==='' || key==='url') continue;
    rows.push({key,label:metricLabel(key),value:formatReportingMetadataValue(key,value),raw:rawMetadataValue(value)});
  }
  return rows.slice(0,36);
}

function formatReportingMetadataValue(key,value){
  if(Array.isArray(value)){
    if(key==='source_document_ids') return `${fmt(value.length)} source document${value.length===1?'':'s'}`;
    return value.slice(0,5).join(', ')+(value.length>5?` + ${value.length-5} more`: '');
  }
  if(value && typeof value==='object') return JSON.stringify(value);
  if(typeof value==='number') return fmt(value);
  const text=String(value);
  if(key==='checksum_sha256') return text.slice(0,12)+'…';
  return text.length>140?text.slice(0,137)+'…':text;
}

function rawMetadataValue(value){
  if(value && typeof value==='object') return JSON.stringify(value);
  return String(value??'');
}

function reportingUsefulLinks(node,edges,graph){
  const byId=new Map((graph?.nodes||[]).map(n=>[n.id,n]));
  const links=[];
  const addUrl=(url,label,kind='Source document')=>{ if(url && /^https?:\/\//i.test(String(url)) && !links.some(l=>l.url===url)) links.push({url:String(url),label:label||'Original source',kind}); };
  const addItem=(label,kind,detail='')=>{ if(label && !links.some(l=>!l.url&&l.label===label&&l.kind===kind)) links.push({label,kind,detail}); };
  addUrl(node?.url,reportingSourceLinkLabel(node),materialLabel(materialType(node)));
  for(const key of ['url','source_url','original_url','document_url','target_url']) addUrl(node?.metadata?.[key],displayNodeTitle(node),metricLabel(key));
  if(node?.node_type==='DataPointGroup') addItem(`${fmt(node.metadata?.datapoint_count||0)} datapoints`, 'Datapoint summary', (node.metadata?.sample_datapoints||[]).slice(0,3).join(' · '));
  for(const edge of edges||[]){
    addUrl(edge.source_url,relationLabel(edge.edge_type),relationLabel(edge.edge_type));
    for(const key of ['url','source_url','original_url','document_url','target_url']) addUrl(edge.metadata?.[key],relationLabel(edge.edge_type),metricLabel(key));
    const other=byId.get(edge.from_node_id===node?.id?edge.to_node_id:edge.from_node_id);
    if(!other) continue;
    addUrl(other.url,reportingSourceLinkLabel(other),materialLabel(materialType(other)));
    for(const key of ['url','source_url','original_url','document_url','target_url']) addUrl(other.metadata?.[key],displayNodeTitle(other),metricLabel(key));
    if(['Template','TemplateSet','InstructionSet','SourceDocument','DataPointGroup'].includes(other.node_type)) addItem(displayNodeTitle(other), materialLabel(materialType(other)), edge.edge_type==='SUMMARISES_DATAPOINTS'?`${fmt(other.metadata?.datapoint_count||0)} datapoints`:relationLabel(edge.edge_type));
  }
  return links;
}

function reportingSourceLinkLabel(node){
  const md=node?.metadata||{};
  const title=md.source_title || md.annex || displayNodeTitle(node);
  const file=sourceFileName(md.source_local_path || md.source_url || node?.url);
  if(file && title && !String(title).includes(file)) return `${title} · ${file}`;
  return title || file || displayNodeTitle(node);
}

function sourceFileName(value){
  if(!value) return '';
  const raw=String(value).split('#').pop() || String(value);
  try{ return decodeURIComponent(raw.split('/').pop()||''); }catch{return raw.split('/').pop()||'';}
}

function ValidationDashboard({data,busy}){
  const [activeQueue,setActiveQueue]=useState('feedback');
  const [feedbackQueue,setFeedbackQueue]=useState({items:[],runs:[],counts:{}});
  const [feedbackBusy,setFeedbackBusy]=useState(false);
  const [query,setQuery]=useState('');
  const [selectedRow,setSelectedRow]=useState(null);
  const [reviewChoices,setReviewChoices]=useState({});

  useEffect(()=>{ loadFeedbackQueue(); },[]);
  useEffect(()=>{ setQuery(''); setSelectedRow(null); },[activeQueue]);

  if(busy&&!data) return <section className="quality quality-workbench"><div className="quality-loading">Loading quality queues…</div></section>;
  if(!data) return <section className="quality quality-workbench"><div className="quality-loading">Open Quality to load queues.</div></section>;

  const queues=buildQualityQueues({validation:data,feedback:feedbackQueue});
  const active=queues.find(q=>q.id===activeQueue)||queues[0];
  const unverifiedRows=active.id==='unverified-links'?filterQueueRows(active.rows||[],query):[];
  const selected=selectedRow || unverifiedRows[0] || null;

  async function loadFeedbackQueue(){
    try{ setFeedbackQueue(await fetchJson(API_BASE+'/feedback')); }
    catch(err){ setFeedbackQueue(prev=>({...prev,last_error:err.message||String(err)})); }
  }
  async function processFeedbackQueue(){
    setFeedbackBusy(true);
    try{
      const result=await fetchJson(API_BASE+'/feedback/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit:3})});
      const refreshed=await fetchJson(API_BASE+'/feedback');
      setFeedbackQueue({...refreshed,last_process:result});
    }catch(err){ setFeedbackQueue(prev=>({...prev,last_error:err.message||String(err)})); }
    finally{ setFeedbackBusy(false); }
  }

  return <section className="quality quality-workbench">
    <div className="quality-topline">
      <div><span>Quality</span><strong>{active.label}</strong></div>
      <p>{active.description}</p>
    </div>
    <div className="quality-workspace">
      <nav className="quality-queue-rail" aria-label="Quality queues">
        {queues.map(queue=>{
          const summary=summariseQueue(queue);
          return <button key={queue.id} type="button" className={queue.id===active.id?'on':''} onClick={()=>setActiveQueue(queue.id)}>
            <strong>{queue.label}</strong>
            <span>{summary.primary}</span>
            <em>{summary.secondary}</em>
          </button>;
        })}
      </nav>
      <main className="quality-workflow" aria-label={`${active.label} workflow`}>
        {active.id==='feedback'
          ? <FeedbackQueueWorksurface queue={feedbackQueue} busy={feedbackBusy} onRefresh={loadFeedbackQueue} onProcess={processFeedbackQueue}/>
          : <UnverifiedLinksWorksurface rows={unverifiedRows} query={query} setQuery={setQuery} selected={selected} setSelected={setSelectedRow} choices={reviewChoices} setChoices={setReviewChoices}/>
        }
      </main>
    </div>
  </section>;
}

function FeedbackQueueWorksurface({queue,busy,onRefresh,onProcess}){
  const items=(queue?.items||[]).slice().reverse();
  const pending=(queue?.items||[]).filter(item=>['pending','failed'].includes(item.status));
  return <div className="queue-surface feedback-surface">
    <div className="workflow-bar">
      <div><span>Node feedback queue</span><strong>{fmt(pending.length)} item(s) ready to run</strong></div>
      <div className="workflow-actions"><button type="button" onClick={onRefresh}>Refresh</button><button type="button" className="primary" onClick={onProcess} disabled={busy||!pending.length}>{busy?'Running…':'Process queue'}</button></div>
    </div>
    {queue?.last_error&&<div className="quality-error">{queue.last_error}</div>}
    {queue?.last_process&&<div className="quality-runline">Last run processed {fmt(queue.last_process.processed)} item(s).</div>}
    <div className="feedback-ledger">
      <div className="ledger-head"><span>Status</span><span>Node</span><span>Feedback</span><span>Result</span></div>
      {items.length?items.map(item=><article key={item.id} className={`ledger-row ${item.status}`}>
        <b>{item.status}</b>
        <strong>{displayNodeTitle(item.node||{})}</strong>
        <p>{truncate(item.feedback,260)}</p>
        <ExpandableResult text={item.last_result||''}/>
      </article>):<div className="empty-workflow">No node feedback queued.</div>}
    </div>
    {(queue?.runs||[]).length>0&&<details className="run-history"><summary>Run history</summary>{queue.runs.slice().reverse().slice(0,8).map(run=><p key={run.id}><b>{run.status}</b><span>{run.feedback_id}</span>{truncate(run.result||'',260)}</p>)}</details>}
  </div>;
}


function ExpandableResult({text}){
  const [open,setOpen]=useState(false);
  if(!text) return <small>—</small>;
  const canExpand=text.length>260 || text.includes('\n');
  return <div className={`result-cell ${open?'open':'collapsed'}`}>
    <small>{open?text:truncate(text,260)}</small>
    {canExpand&&<button type="button" onClick={()=>setOpen(v=>!v)}>{open?'Hide full result':'Show full result'}</button>}
  </div>;
}

function UnverifiedLinksWorksurface({rows,query,setQuery,selected,setSelected,choices,setChoices}){
  const [saving,setSaving]=useState(false);
  const rowKey=row=>row?.sample_id||row?.edge_id||row?.target_id||`${row?.source_title||''}:${row?.target_title||''}`;
  const draft={decision:selected?.review_decision||'',replacement_url:selected?.review_replacement_url||'',rulebook_target:selected?.review_rulebook_target||'',note:selected?.review_note||'',...(choices[rowKey(selected)]||{})};
  function setDraft(patch){ if(selected) setChoices(prev=>({...prev,[rowKey(selected)]:{...draft,...patch}})); }
  async function saveDecision(){
    if(!selected || !draft.decision) return;
    setSaving(true);
    try{
      const res=await fetch(API_BASE+'/validation/unresolved-reference-review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_id:selected.target_id,edge_id:selected.edge_id,sample_id:selected.sample_id,decision:draft.decision,replacement_url:draft.replacement_url||'',rulebook_target:draft.rulebook_target||'',note:draft.note||''})});
      if(!res.ok) throw new Error(await res.text());
      setDraft({saved:true});
    }catch(err){ alert(err.message||String(err)); }
    finally{ setSaving(false); }
  }
  return <div className="queue-surface links-surface">
    <div className="workflow-bar">
      <div><span>Unverified links</span><strong>{fmt(rows.length)} unresolved relationship(s)</strong></div>
      <div className="workflow-actions"><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Filter by node, URL, source or target…"/><button type="button" onClick={()=>downloadCsv('unverified-links.csv',rows)}>Export</button></div>
    </div>
    <div className="links-workgrid">
      <div className="link-ledger" role="list">
        {rows.length?rows.map(row=><button key={rowKey(row)} type="button" className={rowKey(row)===rowKey(selected)?'on':''} onClick={()=>setSelected(row)}>
          <span>{row.target_type||'link'}</span>
          <strong>{row.target_title||row.target_url||'Untitled target'}</strong>
          <em>{row.source_title||row.source_container_title||'Unknown source'}</em>
        </button>):<div className="empty-workflow">No unverified links match this filter.</div>}
      </div>
      <article className="link-review-sheet">
        {selected?<>
          <header><span>{selected.target_type||'unverified'}</span><h3>{selected.target_title||selected.target_url||'Untitled target'}</h3><p>{selected.source_title||selected.source_container_title||'Unknown source'}</p></header>
          <dl className="link-facts">
            <div><dt>Source node</dt><dd>{selected.source_title||'—'}</dd></div>
            <div><dt>Target id</dt><dd>{selected.target_id||'—'}</dd></div>
            <div><dt>Confidence</dt><dd>{selected.confidence!==undefined?cell(selected.confidence,'confidence'):'—'}</dd></div>
            <div><dt>Original method</dt><dd>{selected.original_source_method||selected.source_method||'—'}</dd></div>
          </dl>
          <blockquote>{selected.source_text||selected.evidence_text||'No source text captured.'}</blockquote>
          <div className="link-openers">
            {selected.source_url&&<a href={selected.source_url} target="_blank" rel="noopener noreferrer">Open source</a>}
            {selected.target_url&&<a href={selected.target_url} target="_blank" rel="noopener noreferrer">Open target</a>}
          </div>
          <div className="decision-strip">
            {[['resolved','Resolved'],['external-valid','External valid'],['broken','Broken'],['not-relevant','Not relevant'],['no-longer-valid','No longer valid'],['not-a-link','Not a link']].map(([value,label])=><button key={value} type="button" className={draft.decision===value?'on':''} onClick={()=>setDraft({decision:value})}>{label}</button>)}
          </div>
          <div className="decision-fields">
            <input value={draft.rulebook_target||''} onChange={e=>setDraft({rulebook_target:e.target.value})} placeholder="Rulebook target, if resolved internally"/>
            <input value={draft.replacement_url||''} onChange={e=>setDraft({replacement_url:e.target.value})} placeholder="Replacement URL, if needed"/>
            <textarea value={draft.note||''} onChange={e=>setDraft({note:e.target.value})} placeholder="Short finding"/>
            <button type="button" className="primary" disabled={saving||!draft.decision} onClick={saveDecision}>{saving?'Saving…':draft.saved?'Saved':'Save finding'}</button>
          </div>
        </>:<div className="empty-workflow">Select a link to review.</div>}
      </article>
    </div>
  </div>;
}

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

function Graph({graph,selected,detail,nodeTypes,relationshipTypes,relationshipFilters,materialFilters=MATERIAL_FILTERS,availableEdgeTypes,onToggleNodeType,onToggleRelationship,onSelect,onOpen,onFeedback}){
  const fgRef=useRef(null);
  const wrapRef=useRef(null);
  const lastClickRef=useRef({id:null,time:0});
  const [hover,setHover]=useState(null);
  const [hoverEdge,setHoverEdge]=useState(null);
  const [contextMenu,setContextMenu]=useState(null);
  const [graphSize,setGraphSize]=useState({width:0,height:0});
  const data=useMemo(()=>forceGraphData(graph,selected),[graph,selected?.id]);
  const graphDensity=forceGraphDensity(data);

  useEffect(()=>{
    const el=wrapRef.current;
    if(!el || typeof ResizeObserver==='undefined') return;
    const setSize=(width,height)=>setGraphSize(prev=>{
      const next={width:Math.max(1,Math.floor(width||0)),height:Math.max(1,Math.floor(height||0))};
      return prev.width===next.width&&prev.height===next.height?prev:next;
    });
    const rect=el.getBoundingClientRect();
    setSize(rect.width,rect.height);
    const ro=new ResizeObserver(([entry])=>{
      const box=entry.contentRect;
      setSize(box.width,box.height);
    });
    ro.observe(el);
    return ()=>ro.disconnect();
  },[]);

  useEffect(()=>{
    const fg=fgRef.current;
    if(!fg || !graphSize.width || !graphSize.height) return;
    fg.d3Force('collide',forceCollide(node=>forceNodeCollisionRadius(node)).strength(.88));
    fg.d3Force('x',forceX(node=>forceNodeTargetX(node)).strength(node=>forceNodeAxisStrength(node)));
    fg.d3Force('y',forceY(node=>forceNodeTargetY(node)).strength(node=>forceNodeAxisStrength(node)));
    fg.d3Force('charge')?.strength(-260);
    fg.d3Force('link')?.distance(edge=>edge.edge_type==='contains'?90:160).strength(edge=>edge.edge_type==='contains'?.45:.12);
    const id=detail?.id||selected?.id;
    const node=id?data.nodes.find(n=>n.id===id):null;
    setTimeout(()=>node?frameNode(fg,node,420):fg.zoomToFit(420,70),260);
  },[data,detail?.id,selected?.id,graphSize.width,graphSize.height]);

  useEffect(()=>{
    const fg=fgRef.current;
    if(!fg || !graphSize.width || !graphSize.height) return;
    const id=detail?.id||selected?.id;
    if(!id) return;
    const node=data.nodes.find(n=>n.id===id);
    if(node) frameNode(fg,node,420);
  },[detail?.id,selected?.id,data,graphSize.width,graphSize.height]);

  function frameNode(fg,node,duration=360){
    fg.centerAt(node.x||0,node.y||0,duration);
    fg.zoom(1.35,duration);
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
  function openContextMenu(node,event){
    event?.preventDefault?.();
    const raw=node.raw||node;
    setContextMenu({node:raw,x:event?.clientX||window.innerWidth/2,y:event?.clientY||window.innerHeight/2});
  }
  function clickNode(node){
    setContextMenu(null);
    const now=Date.now();
    const last=lastClickRef.current;
    if(last.id===node.id && now-last.time<420){ lastClickRef.current={id:null,time:0}; onOpen(node.raw||node); }
    else { lastClickRef.current={id:node.id,time:now}; onSelect(node.raw||node); }
  }

  return <div ref={wrapRef} className="graph-wrap forcegraph-wrap">
    <ForceGraph2D
      ref={fgRef}
      graphData={data}
      width={graphSize.width}
      height={graphSize.height}
      backgroundColor="rgba(0,0,0,0)"
      nodeRelSize={1}
      nodeId="id"
      nodeVal={node=>node.size}
      nodeLabel={node=>displayNodeTitle(node.raw||node)}
      nodeCanvasObject={(node,ctx,globalScale)=>drawGraphNode(node,ctx,globalScale,selected,graphDensity)}
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
      onNodeRightClick={openContextMenu}
      onBackgroundClick={()=>setContextMenu(null)}
      onNodeHover={node=>setHover(node?.raw||node||null)}
      onLinkHover={edge=>setHoverEdge(edge||null)}
      cooldownTicks={140}
      d3VelocityDecay={0.32}
      warmupTicks={80}
    />
    {contextMenu&&<div className="node-context-menu" style={{left:contextMenu.x,top:contextMenu.y}}><button type="button" onClick={()=>{onFeedback?.(contextMenu.node);setContextMenu(null);}}>Provide feedback on this node</button><button type="button" onClick={()=>{onOpen(contextMenu.node);setContextMenu(null);}}>Open / drill into node</button></div>}
    {hover&&<div className="node-tip forcegraph-tip"><span>{materialLabel(materialType(hover))}</span><strong>{displayNodeTitle(hover)}</strong><small>{truncate(hover.text||hover.url||'',180)}</small><small>Click to inspect · double-click to open/drill · right-click for feedback</small></div>}
    {hoverEdge&&<div className="node-tip forcegraph-tip edge-tip"><span>{edgeTooltip(hoverEdge,selected?.id)}</span><strong>{relationLabel(hoverEdge.edge_type)}</strong><small>{truncate(edgeTerm(hoverEdge)||edgeSummary(hoverEdge,selected?.id),180)}</small></div>}
    <Legend active={nodeTypes} materialFilters={materialFilters} relationshipTypes={relationshipTypes} relationshipFilters={relationshipFilters} availableEdgeTypes={availableEdgeTypes} onToggle={onToggleNodeType} onToggleRelationship={onToggleRelationship} />
    <div className="nav-help">Drag to pan · scroll to zoom · click to inspect · double-click to open/drill</div>
    <div className="zoom"><button title="Zoom in" onClick={()=>zoom(1.18)}>＋</button><button title="Zoom out" onClick={()=>zoom(.86)}>−</button><button title="Fit graph" onClick={fit}>⤢</button><button title="Focus selected" onClick={()=>focusNode(detail||selected)}>◎</button></div>
  </div>;
}

function forceGraphData(graph,selected){
  const nodes=(graph.nodes||[]).map(node=>{
    const role=relativeNodeRole(node,selected?.id,graph);
    return {...node,raw:node,id:node.id,role,layoutLane:forceNodeLayoutLane({...node,role},selected?.id,graph.edges||[]),badge:documentBadge(node),colour:nodeFill(node,graph),size:forceNodeSize(node,graph,selected),degree:node.degree||node.metadata?.weighted_degree||1};
  });
  const ids=new Set(nodes.map(n=>n.id));
  const visibleEdges=(graph.edges||[]).filter(edge=>ids.has(edge.from_node_id)&&ids.has(edge.to_node_id));
  const links=collapseParallelEdges(visibleEdges).map((edge,i)=>({...edge,id:edge.id||`${edge.from_node_id}-${edge.to_node_id}-${edge.edge_type}-${i}`,source:edge.from_node_id,target:edge.to_node_id,direction:edgeDirectionLabel(edge,selected?.id),curveDistance:0}));
  return {nodes,links};
}
function forceNodeSize(node,graph,selected){
  if(node.id===selected?.id||node.id===graph?.centre_id) return 18;
  const base=r(node,graph);
  return Math.max(8,Math.min(18,base*.72));
}
function forceNodeCollisionRadius(node){
  const busyBonus=Math.min(34,Math.log2(Math.max(1,node.degree||1))*7);
  const labelBonus=node.badge||node.role==='parent'?8:0;
  return Math.max(22,node.size||22)+10+busyBonus+labelBonus;
}
function forceNodeLayoutLane(node,selectedId,edges){
  if(!selectedId || node.id===selectedId) return 'centre';
  if(['defined_term','glossary','crr_terms_list'].includes(node.node_type)) return 'northEast';
  if(node.role==='parent') return 'north';
  if(node.role==='child') return 'south';
  if(node.node_type==='part' || node.node_type==='rulebook') return 'north';
  const incident=(edges||[]).filter(edge=>edge.from_node_id===node.id||edge.to_node_id===node.id);
  for(const edge of incident){
    if(edge.edge_type==='references' && edge.to_node_id===selectedId) return 'west';
    if(edge.edge_type==='references' && edge.from_node_id===selectedId) return 'east';
    if(edge.from_node_id===selectedId && isPurpleAnalysisNode(node)) return 'east';
  }
  return 'related';
}
function isPurpleAnalysisNode(node){
  return ['obligation_pattern','obligation_statement'].includes(node.node_type);
}
function forceNodeTargetX(node){
  if(node.layoutLane==='west') return -280;
  if(node.layoutLane==='east') return 280;
  if(node.layoutLane==='northEast') return 260;
  return 0;
}
function forceNodeTargetY(node){
  if(node.layoutLane==='north') return -220;
  if(node.layoutLane==='northEast') return -220;
  if(node.layoutLane==='south') return 240;
  return 0;
}
function forceNodeAxisStrength(node){
  if(node.layoutLane==='centre') return .22;
  if(['north','northEast','south','west','east'].includes(node.layoutLane)) return .105;
  return .018;
}
function drawGraphNode(node,ctx,globalScale,selected,graphDensity){
  const raw=node.raw||node;
  const radius=node.size||10;
  const badge=node.badge;
  const role=node.role;
  const visual=reportingVisualKind(raw);
  const selectedNode=raw.id===selected?.id;
  ctx.save();
  ctx.beginPath();
  if(visual==='template'){ reportingTemplatePath(ctx,node.x,node.y,radius); }
  else if(visual==='instruction'){ hexPath(ctx,node.x,node.y,radius*1.18); }
  else if(visual==='xbrl_source'){ sourceCylinderPath(ctx,node.x,node.y,radius); }
  else if(badge){ roundedRectPath(ctx,node.x-radius*1.45,node.y-radius*.85,radius*2.9,radius*1.7,5); }
  else if(role==='parent'){ ctx.rect(node.x-radius,node.y-radius,radius*2,radius*2); }
  else if(role==='child'){ ctx.moveTo(node.x,node.y-radius*1.2); ctx.lineTo(node.x+radius*1.15,node.y); ctx.lineTo(node.x,node.y+radius*1.2); ctx.lineTo(node.x-radius*1.15,node.y); ctx.closePath(); }
  else { ctx.arc(node.x,node.y,radius,0,Math.PI*2); }
  ctx.fillStyle=visual==='template'?'#dcfce7':visual==='instruction'?'#fef3c7':visual==='xbrl_source'?'#ede9fe':badge?.kind==='pdf'?'#b91c1c':badge?.kind==='spreadsheet'?'#047857':role==='parent'?'#fff1f2':node.colour||nodeFill(raw,{});
  ctx.fill();
  ctx.lineWidth=selectedNode?4:visual?3:role==='parent'?3:2;
  ctx.setLineDash(!visual&&(role==='parent'||role==='child')?[4,3]:[]);
  ctx.strokeStyle=selectedNode?'#2457d6':visual==='template'?'#047857':visual==='instruction'?'#d97706':visual==='xbrl_source'?'#6d28d9':role==='parent'?'#be123c':role==='child'?'#2563eb':'#ffffff';
  ctx.stroke();
  ctx.setLineDash([]);
  drawReportingNodeGlyph(ctx,visual,node.x,node.y,radius,globalScale);
  const importantNode=isImportantForceNode(node);
  const denseSmallLabel=graphDensity==='dense' && !importantNode;
  const label=forceGraphNodeLabel(node,selected,globalScale,graphDensity);
  if(label){ drawCanvasLabel(ctx,label,node.x,node.y+radius+9/globalScale,denseSmallLabel?7:(selectedNode?12:10),globalScale,selectedNode,denseSmallLabel?6.25:8); }
  ctx.restore();
}

function reportingVisualKind(node){
  const type=materialType(node);
  if(type==='reporting_template') return 'template';
  if(type==='reporting_instruction') return 'instruction';
  if(type==='reporting_xbrl_source') return 'xbrl_source';
  return '';
}
function reportingTemplatePath(ctx,x,y,r){
  roundedRectPath(ctx,x-r*1.25,y-r*.95,r*2.5,r*1.9,4);
  ctx.moveTo(x+r*.45,y-r*.95); ctx.lineTo(x+r*1.25,y-r*.2); ctx.lineTo(x+r*.45,y-r*.2); ctx.closePath();
}
function hexPath(ctx,x,y,r){
  for(let i=0;i<6;i++){ const a=Math.PI/6+i*Math.PI/3; const px=x+Math.cos(a)*r, py=y+Math.sin(a)*r; i?ctx.lineTo(px,py):ctx.moveTo(px,py); }
  ctx.closePath();
}
function sourceCylinderPath(ctx,x,y,r){
  roundedRectPath(ctx,x-r*1.12,y-r*.9,r*2.24,r*1.8,r*.5);
}
function drawReportingNodeGlyph(ctx,visual,x,y,r,globalScale){
  if(!visual) return;
  ctx.save();
  ctx.lineWidth=Math.max(1,1.35/globalScale);
  ctx.strokeStyle=visual==='template'?'#047857':visual==='instruction'?'#92400e':'#5b21b6';
  ctx.fillStyle=ctx.strokeStyle;
  if(visual==='template'){
    for(const dy of [-.25,.08,.41]){ ctx.beginPath(); ctx.moveTo(x-r*.55,y+r*dy); ctx.lineTo(x+r*.35,y+r*dy); ctx.stroke(); }
  }else if(visual==='instruction'){
    ctx.beginPath(); ctx.arc(x,y-r*.18,r*.12,0,Math.PI*2); ctx.fill(); ctx.beginPath(); ctx.moveTo(x,y+r*.02); ctx.lineTo(x,y+r*.52); ctx.stroke();
  }else if(visual==='xbrl_source'){
    ctx.beginPath(); ctx.ellipse(x,y-r*.42,r*.55,r*.18,0,0,Math.PI*2); ctx.stroke(); ctx.beginPath(); ctx.moveTo(x-r*.55,y-r*.42); ctx.lineTo(x-r*.55,y+r*.38); ctx.moveTo(x+r*.55,y-r*.42); ctx.lineTo(x+r*.55,y+r*.38); ctx.stroke(); ctx.beginPath(); ctx.ellipse(x,y+r*.38,r*.55,r*.18,0,0,Math.PI); ctx.stroke();
  }
  ctx.restore();
}
function forceGraphNodeLabel(node,selected,globalScale,graphDensity){
  const raw=node.raw||node;
  if(raw.id===selected?.id) return truncate(displayNodeTitle(raw),42);
  if(globalScale<.65) return '';
  const importantNode=isImportantForceNode(node);
  if(graphDensity==='dense' && !importantNode) return truncate(displayNodeTitle(raw),18);
  if(node.badge) return truncate(displayNodeTitle(raw),30);
  if(node.role==='parent'||node.role==='child') return truncate(displayNodeTitle(raw),28);
  if((node.degree||0)>=4 || ['part','chapter','guidance_document','defined_term'].includes(raw.node_type)) return truncate(displayNodeTitle(raw),30);
  if(globalScale>1.05) return truncate(displayNodeTitle(raw),24);
  return '';
}
function isImportantForceNode(node){
  const raw=node.raw||node;
  return Boolean(node.badge || node.role==='parent' || (node.degree||0)>=8 || ['part','guidance_document','defined_term'].includes(raw.node_type));
}
function forceGraphDensity(data){
  const nodes=data?.nodes?.length||0;
  const links=data?.links?.length||0;
  if(nodes>=70 || links>=150 || links/Math.max(1,nodes)>2.4) return 'dense';
  return 'normal';
}
function drawGraphLink(edge,ctx,globalScale,selected){
  if(edge.edge_type==='contains') return;
  const sx=edge.source.x, sy=edge.source.y, tx=edge.target.x, ty=edge.target.y;
  if(!Number.isFinite(sx+sy+tx+ty)) return;
  if(edge.parallelCount>1) drawParallelEdgeCount(edge,ctx,globalScale);
}
function drawParallelEdgeCount(edge,ctx,globalScale){
  const sx=edge.source.x, sy=edge.source.y, tx=edge.target.x, ty=edge.target.y;
  const x=(sx+tx)/2;
  const y=(sy+ty)/2;
  const label=String(edge.parallelCount);
  const size=Math.max(9,11/globalScale);
  ctx.save();
  ctx.font=`800 ${size}px Inter, system-ui, sans-serif`;
  ctx.textAlign='center'; ctx.textBaseline='middle';
  const width=Math.max(18/globalScale,ctx.measureText(label).width+10/globalScale);
  const height=Math.max(16/globalScale,size+6/globalScale);
  ctx.fillStyle='rgba(36,87,214,.96)';
  roundedRectPath(ctx,x-width/2,y-height/2,width,height,height/2); ctx.fill();
  ctx.strokeStyle='rgba(255,255,255,.92)'; ctx.lineWidth=1.5/globalScale; ctx.stroke();
  ctx.fillStyle='#fff'; ctx.fillText(label,x,y+.5/globalScale);
  ctx.restore();
}
function drawCanvasLabel(ctx,text,x,y,fontSize,globalScale,strong=false,minFontSize=8){
  const size=Math.max(minFontSize,fontSize/globalScale);
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
function collapseParallelEdges(edges){
  const grouped=new Map();
  for(const edge of edges||[]){
    const key=parallelEdgeKey(edge);
    if(!grouped.has(key)) grouped.set(key,{...edge,parallelCount:1,parallelEdges:[edge],confidence:edge.confidence||0});
    else{
      const current=grouped.get(key);
      current.parallelCount+=1;
      current.parallelEdges.push(edge);
      current.confidence=Math.max(current.confidence||0,edge.confidence||0);
      current.evidence_text=current.evidence_text||edge.evidence_text;
      current.metadata={...(current.metadata||{}),parallel_edge_ids:current.parallelEdges.map(e=>e.id).filter(Boolean)};
    }
  }
  return [...grouped.values()];
}



function Legend({active,materialFilters=MATERIAL_FILTERS,relationshipTypes,relationshipFilters,availableEdgeTypes,onToggle,onToggleRelationship}){
  return <div className="legend" aria-label="Graph filters">
    <div className="legend-title">Node types</div>
    {materialFilters.map(t=><button type="button" key={t} className={materialFilterOn(t,active)?'on':'off'} onClick={()=>onToggle(t)} title={`Toggle ${materialLabel(t)}`}><i className={`legend-node ${legendNodeIcon(t)}`} style={{background:displayColour(t),borderColor:displayColour(t)}} />{materialLabel(t)}</button>)}
    <div className="legend-title">Edge types</div>
    {relationshipFilters.map(t=><button type="button" key={t} className={relationshipTypes?.has(t)?'on':'off'} onClick={()=>onToggleRelationship(t)} title={`Toggle ${relationLabel(t)}`}><i className={`line ${t==='contains'?'dash':''}`} style={{borderColor:edgeColour(t)}} />{relationLabel(t)}<em>{availableEdgeTypes?.[t]??''}</em></button>)}
  </div>;
}

function legendNodeIcon(t){
  if(t==='reporting_template') return 'template';
  if(t==='reporting_instruction') return 'instruction';
  if(t==='reporting_xbrl_source') return 'xbrl-source';
  if(t==='reporting_return') return 'return';
  return '';
}

function parallelEdgeKey(edge){
  const a=edge.from_node_id||edge.source;
  const b=edge.to_node_id||edge.target;
  return `${a}→${b}→${edge.edge_type||''}`;
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
      : <Collapsible title="Visible connections" count="0 links" open><p className="muted">No reference, definition or obligation links are visible for this node under the current settings.</p></Collapsible>}
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
  const priority=['has_permission','references','uses_defined_term','defines','shares_defined_term','has_obligation_pattern','shares_obligation_pattern','has_structured_obligation','amends'];
  const buckets=new Map();
  for(const e of edges) buckets.set(e.edge_type,[...(buckets.get(e.edge_type)||[]),e]);
  return [...buckets.entries()].sort((a,b)=>{
    const ai=priority.indexOf(a[0]), bi=priority.indexOf(b[0]);
    return (ai<0?99:ai)-(bi<0?99:bi) || b[1].length-a[1].length || a[0].localeCompare(b[0]);
  });
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
  return Math.min(25,(n.node_type==='part'?14:n.node_type==='defined_term'?11:9)+Math.sqrt(n.degree||1));
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
function isInferred(e){return !EXPLICIT.has(e.source_method) && !String(e.source_method||'').startsWith('reporting') && !['manifest','pdf_text_extraction'].includes(e.source_method)}
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
    manifest:'reporting manifest',
    reporting_llm_reference:'reporting reference extraction',
    pdf_text_extraction:'PDF/text extraction',
  }[method]||String(method||'').replaceAll('_',' '));
}
function edgeSummary(e,currentId){
  const confidence=`${Math.round((e.confidence||0)*100)}%`;
  const direction=currentId?`${edgeDirectionLabel(e,currentId)} · `:'';
  const count=e.parallelCount>1?`${e.parallelCount} references · `:'';
  return `${direction}${count}${relationLabel(e.edge_type)} · ${provenanceLabel(e.source_method)} · ${confidence}`;
}
function edgeTerm(e){
  return e.metadata?.term_title || e.evidence_text || e.metadata?.reference || e.metadata?.target_title || '';
}
function edgeTooltip(e,currentId){
  const term=edgeTerm(e);
  const direction=currentId?`${edgeDirectionLabel(e,currentId)} `:'';
  const count=e.parallelCount>1?`${e.parallelCount} references · `:'';
  return term ? `${direction}${count}${relationLabel(e.edge_type)}: ${term}` : edgeSummary(e,currentId);
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
  if(type==='DataItem') return 'reporting_return';
  if(type==='Template') return 'reporting_template';
  if(type==='TemplateSet') return 'reporting_xbrl_source';
  if(type==='InstructionSet') return 'reporting_instruction';
  if(type==='SourceDocument') return isXbrlSourceDocument(n)?'reporting_xbrl_source':'reporting_source';
  if(type==='DataPointGroup' || type==='DataPoint' || type==='TemplateRow' || type==='TemplateColumn') return 'reporting_datapoint';
  if(type==='Provision') return 'reporting_provision';
  if(['Concept','ValidationRule','ScopeRule','FirmType','Metric','CalculationRule'].includes(type)) return 'reporting_concept';
  if(['ExternalReference'].includes(type)) return 'external_reference';
  if(['LegalInstrument'].includes(type)) return 'legal_instrument';
  if(['Permission'].includes(type)) return 'permission';
  if(['rule','chapter','part','rulebook'].includes(type)) return 'rule';
  if(['defined_term','glossary','crr_terms_list'].includes(type)) return 'definition';
  if(type==='legal_instrument') return 'legal_instrument';
  if(type==='permission') return 'permission';
  if(type==='external_reference' || type==='rule_reference') return 'external_reference';
  if(['obligation_pattern','obligation_statement'].includes(type)) return type;
  if(['guidance_document','guidance_section','guidance_paragraph'].includes(type)){
    if(doc.includes('statement_of_policy') || url.includes('/statements-of-policy/')) return 'statement_of_policy';
    return 'supervisory_statement';
  }
  return type||'external_reference';
}
function isXbrlSourceDocument(n){
  const md=(typeof n==='string'?{}:n?.metadata)||{};
  const hay=[n?.title,n?.text,n?.url,md.source_url,md.source_local_path,md.source_title,md.file_type,md.source_file_type,md.source_table,md.source_pk].filter(Boolean).join(' ').toLowerCase();
  return /\b(xbrl|taxonomy|dpm|annotated templates|template package|reporting package)\b/.test(hay) || /\.(zip|xbrl|xml|xsd)(#|$)/.test(hay);
}
function materialLabel(v){return ({rule:'Rulebook part / rule',supervisory_statement:'Supervisory statement',statement_of_policy:'Statement of policy',definition:'Definition',permission:'Firm permission',external_reference:'External reference',legal_instrument:'Legal instrument',obligation_pattern:'Obligation pattern',obligation_statement:'Structured obligation',analysis:'Obligation marker',reporting_return:'Reporting return',reporting_template:'Reporting template',reporting_instruction:'Reporting instructions',reporting_source:'Source document',reporting_xbrl_source:'XBRL source',reporting_datapoint:'Datapoints',reporting_provision:'Referenced provision',reporting_concept:'Reporting concept',DataItem:'Reporting return',Template:'Reporting template',TemplateSet:'XBRL source',InstructionSet:'Reporting instructions',SourceDocument:'Source document',DataPointGroup:'Datapoint summary',DataPoint:'Datapoint',Provision:'Referenced provision'}[v]||String(v||'').replaceAll('_',' '))}
function displayColour(v){return MATERIAL_COLOURS[materialType(v)]||'#64748b'}
function label(v){return materialLabel(materialType(v))}
function truncate(s='',n=120){return s&&s.length>n?s.slice(0,n-1)+'…':s}

createRoot(document.getElementById('root')).render(<App/>);
