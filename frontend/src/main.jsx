import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import ForceGraph2D from 'react-force-graph-2d';
import { forceCollide, forceX, forceY } from 'd3-force';
import { filterGraph, isInsuranceNode } from './graphFilters.js';
import { displayNodeTitle, documentBadge, relativeNodeRole, edgeDirectionGlyph, edgeDirectionLabel } from './graphPresentation.js';
import { CHART_SEQUENCE, COLOURS, EDGE_COLOURS, MATERIAL_COLOURS } from './colourTokens.js';
import {
  REPORTING_EDGE_GROUPS,
  REPORTING_OVERVIEW_EDGE_GROUP_KEYS,
  REPORTING_REQUIREMENT_EDGE_GROUP_KEYS,
  reportingEditionOptionLabel,
  reportingChildGroups,
  reportingEdgeGroup,
  reportingEdgeGroupCounts,
  reportingEdgeTypesForGroups,
  reportingOneHopGraph,
  reportingParentNodes,
  reportingRequirementEditions,
  reportingSourceNodes,
} from './reportingNavigation.js';
import {
  reportingCellCoordinate,
  reportingCellCoverage,
  reportingCellPath,
  reportingCellTitle,
  reportingNodeSelectsTemplate,
  reportingTemplateForNode,
  reportingTemplateGrid,
  reportingTemplateTitle,
  reportingWorkbookCellStyle,
  reportingWorkbookColumnPixels,
  reportingWorkbookDatapoints,
} from './reportingCells.js';
import {
  assignReferencesToParagraphs,
  legalTextBlocks,
  mergeOverlappingReferences,
  paragraphCitationSegments,
  readerReferences,
  readingSpine,
  referenceDisplayTitle,
  referenceShelfDensity,
} from './readingMode.js';
import { filterIssues, issueCounts, issueDateLabel, issueStatusLabel, ISSUE_STATUSES } from './issuesLog.js';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || '/pra-rulebook-api';
const TYPES = ['contains','references','uses_defined_term','defines','shares_defined_term','has_obligation_pattern','has_structured_obligation','shares_obligation_pattern','amends','has_permission','has_version','sourced_from'];
const PROVISION_TYPES = ['rule','provision','chapter','guidance_section','guidance_paragraph'];
const NODE_TYPES = [...PROVISION_TYPES,'part','rulebook','defined_term','glossary','crr_terms_list','guidance_document','obligation_pattern','obligation_statement','legal_instrument','permission','external_reference','rule_reference'];
const DEFAULT_TYPES = new Set(['contains','references']);
const REPRESENTATIONS = {
  combined: { label:'Combined', hint:'Legal structure plus rolled-up references, terms, obligations and permissions.', types:[...DEFAULT_TYPES], depth:1, explicitOnly:false },
  hierarchy: { label:'Legal hierarchy', hint:'Parts, articles, chapters, rules and paragraphs only.', types:['contains'], depth:2, explicitOnly:false },
  references: { label:'Cross-references', hint:'Explicit and detected cross-reference/amendment links, with child context so Article-level headings expose paragraph-level references.', types:['contains','references','amends'], depth:2, explicitOnly:false },
  definitions: { label:'Definitions', hint:'Definitions, glossary/CRR term usage, and provisions sharing defined terms.', types:['uses_defined_term','defines','shares_defined_term'], depth:2, explicitOnly:false },
  obligations: { label:'Obligations', hint:'Detected obligation statements, obligation patterns, and provisions with similar obligation patterns.', types:['has_obligation_pattern','has_structured_obligation','shares_obligation_pattern'], depth:1, explicitOnly:false },
};
const EXPLICIT = new Set(['site_structure','html_link','html_anchor_resolved','html_glossary_link','glossary_source','crr_terms_source','legal_instrument_listing','legal_reference_occurrence_v1','regex_reference','regex_named_reference','llm_extracted_reference','resolved_part_reference','fca_waivers_list']);
const RELATION_LABELS = { contains:'contains / child', references:'Cross-references', uses_defined_term:'Definitions used', defines:'Definitions provided', shares_defined_term:'Shared defined terms', has_obligation_pattern:'Obligation themes', shares_obligation_pattern:'Similar obligations', has_structured_obligation:'Extracted obligations', amends:'Amendments', has_permission:'Firms with permissions', has_version:'Provision versions', sourced_from:'Source page', HAS_REGIME:'Has regime', HAS_COLLECTION:'Has collection', BELONGS_TO_REGIME:'Belongs to regime', BELONGS_TO_COLLECTION:'Belongs to collection', HAS_EDITION:'Has edition', SUPERSEDES:'Supersedes', HAS_TEMPLATE_RESOURCE:'Has template resource', HAS_INSTRUCTION_RESOURCE:'Has instruction resource', HAS_RESOURCE:'Has resource', CONTAINS_SHEET:'Contains worksheet', IMPLEMENTS_TEMPLATE:'Implements template', SUPPORTED_BY_TAXONOMY:'Supported by taxonomy', HAS_TAXONOMY_RESOURCE:'Has taxonomy resource', USES_TEMPLATE:'Uses template', USES_INSTRUCTIONS:'Uses instructions', EVIDENCED_BY:'Evidenced by', LEGAL_BASIS:'Legal basis', APPLIES_TO:'Applies to', HAS_SCOPE_RULE:'Scope rule', MAY_BE_AFFECTED_BY_PERMISSION:'Affected by permission', REFERENCES_RULE:'References rule', REFERENCES_SOURCE:'References source', REFERENCES_EXTERNAL:'References external', REFERENCES_RETURN:'References return', REFERENCES_TEMPLATE:'References template', SUMMARISES_DATAPOINTS:'Summarises datapoints', HAS_DATAPOINT:'Has datapoint', REPORTS_CONCEPT:'Reports concept' };
const EVIDENCE_LABELS = { references:'Cross-references', uses_defined_term:'Definitions used by this provision', defines:'Definitions provided here', shares_defined_term:'Provisions sharing defined terms', has_obligation_pattern:'Obligation themes found here', shares_obligation_pattern:'Provisions with similar obligations', has_structured_obligation:'Extracted obligation statements', amends:'Legal instruments amending this material', has_permission:'Firms with active permissions' };
const ORIGIN_FILTERS = { all:'All links', explicit:'Direct links', inferred:'Inferred / derived links' };
const MATERIAL_FILTERS = ['rule','supervisory_statement','statement_of_policy','definition','permission','legal_instrument','external_reference'];
const RELATIONSHIP_ORDER = TYPES;
const REPORTING_NODE_TYPES = ['ReportingEstate','ReportingRegime','ReportingCollection','ReportingRequirement','RequirementEdition','ReportingResource','Worksheet','LogicalTemplate','InstructionSection','TaxonomyRelease','TaxonomyEntryPoint','DataItem','ReportingReturn','DisclosureSet','ReportingObligation','Template','InstructionSet','SourceDocument','Provision','ExternalReference','LegalInstrument','PolicyStatement','TemplateSet','DataPointGroup','DataPoint','TemplateRow','TemplateColumn','Concept','ScopeRule','FirmType','Permission','ValidationRule'];
const REPORTING_DEFAULT_EDGE_TYPES = new Set(['USES_TEMPLATE','USES_INSTRUCTIONS','EVIDENCED_BY','LEGAL_BASIS','APPLIES_TO','HAS_SCOPE_RULE','MAY_BE_AFFECTED_BY_PERMISSION','SUMMARISES_DATAPOINTS']);
const PART_AUDIENCE_FILTERS=[
  {key:'crr',label:'CRR firms',category:'CRR Firms'},
  {key:'non-crr',label:'Non-CRR firms',category:'Non-CRR Firms'},
  {key:'no-authorised',label:'No authorised persons',category:'Non-authorised persons'},
  {key:'sii',label:'SII firms',category:'SII Firms'},
  {key:'non-sii',label:'Non-SII firms',category:'Non-SII Firms'},
];

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
  const [partAudienceFilter,setPartAudienceFilter]=useState('all');
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
  const [panelOpen,setPanelOpen]=useState(()=>window.innerWidth>900);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [issueReportNode,setIssueReportNode]=useState(null);
  const [issueText,setIssueText]=useState('');
  const [issueSaving,setIssueSaving]=useState(false);
  const [issueSaved,setIssueSaved]=useState(false);
  const [readingNode,setReadingNode]=useState(null);

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
      const statsPromise=api('/stats').then(data=>setStats(data));
      statsPromise.catch(e=>setError(e.message||String(e)));
      const parts=await api('/nodes?types=part&limit=300&summary=true');
      setResults(parts.results||[]);
      setRailContext(null);
      setRailStack([]);
      const initialNode=parts.results?.[0];
      if(initialNode){
        await choose(initialNode, {drill:false, openPanel:window.innerWidth>900});
      }else{
        const roots=await api('/nodes?types=rulebook&limit=1');
        if(roots.results?.[0]) await choose(roots.results[0], {drill:false, openPanel:window.innerWidth>900});
      }
    }catch(e){setError(e.message||String(e));}
  }
  async function loadAllParts(){
    const data=await api('/nodes?types=part&limit=300&summary=true');
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
    setSelected(full); setDetail(full); setPanelOpen(opts.openPanel ?? window.innerWidth>900);
    const tree=await loadContents(full.id);
    if(opts.drill!==false && tree?.children?.length && ['rulebook','part','chapter','provision','guidance_document','guidance_section'].includes(full.node_type)){
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
  const showPartAudienceFilters=!railContext&&results.some(r=>r.node_type==='part'&&partAudienceCategories(r).length);
  const railResults=useMemo(()=>showPartAudienceFilters?results.filter(r=>partAudienceFilter==='all'||partAudienceCategories(r).includes(partAudienceFilter)):results,[results,showPartAudienceFilters,partAudienceFilter]);
  const selectedEdges=useMemo(()=>visibleGraph.edges.filter(e=>detail&&(e.from_node_id===detail.id||e.to_node_id===detail.id)),[visibleGraph,detail]);
  async function submitIssueReport(nodeOverride){
    const node=nodeOverride||issueReportNode;
    if(!node) return;
    setIssueSaving(true); setError('');
    try{
      const description=(typeof nodeOverride==='string'?issueText:issueText).trim();
      const res=await fetch(API_BASE+'/issues/node',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({node,description,page_url:window.location.href,context:readingNode?'reading_mode':'graph_view'})});
      if(!res.ok) throw new Error(await responseErrorText(res));
      setIssueReportNode(null); setIssueText(''); setIssueSaved(true);
      setTimeout(()=>setIssueSaved(false),2500);
    }catch(err){ setError(err.message||String(err)); }
    finally{ setIssueSaving(false); }
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
      reporting_return:['ReportingRequirement','RequirementEdition','DataItem','ReportingReturn','DisclosureSet'],
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

  function openReadingMode(node){
    if(!node) return;
    setReadingNode(node);
    setView('graph');
    setPanelOpen(false);
  }

  function openIssuesLog(event){
    event?.currentTarget?.closest('details')?.removeAttribute('open');
    setReadingNode(null);
    setIssueReportNode(null);
    setView('issues');
    setPanelOpen(false);
    setError('');
  }

  return <div className={`shell ${panelOpen?'panel-open':'panel-closed'} ${view==='reporting'?'reporting-view-mode':''} ${view==='issues'?'issues-view-mode':''} ${readingNode?'reading-view-mode':''} ${readingNode&&issueReportNode?'reading-issue-open':''}`}>
    <header className="topbar">
      <a className="home" href="/">‹</a>
      <form className="command" onSubmit={search}>
        <span>⌕</span><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search, or leave blank for all Parts" autoFocus/><button>{busy?'…':'Search'}</button>
      </form>
      <div className="top-actions">
        <button className={view==='graph'?'mode on':'mode'} onClick={()=>setView('graph')}>Graph</button>
        <button className={view==='reporting'?'mode on':'mode'} onClick={()=>{setView('reporting');setPanelOpen(false);}}>Reporting</button>
        <button onClick={()=>setPanelOpen(!panelOpen)} title="Toggle side panel">◧</button>
      </div>
    </header>

    <aside className="rail">
      <div className="rail-brand">
        <a className="rail-brand-mark" href="/" aria-label="PRA Rulebook home">
          <img className="rail-brand-logo" src={`${import.meta.env.BASE_URL}pra-rulebook-graph-logo.svg`} alt="" aria-hidden="true" />
        </a>
        <div className="rail-brand-copy"><span>Rulebook Explorer</span><strong>PRA Rulebook</strong></div>
        <a className="rail-collapse" href="/" aria-label="Return to PRA Rulebook home" title="PRA Rulebook home">«</a>
      </div>
      <div className="product"><strong>PRA Rulebook</strong><span>{railContext?`${railContext.kind} · ${railContext.title}`:(q.trim()?'Search results':'All Rulebook Parts')} · {stats?`${stats.nodes.toLocaleString()} nodes`:''}</span><div className="rail-actions">{railStack.length>0&&<button className="back-link" onClick={goUp}>‹ Up one level</button>}{railContext&&<button className="back-link secondary" onClick={loadAllParts}>All Parts</button>}</div></div>
      {error&&<div className="error">{error}</div>}
      {showPartAudienceFilters&&<div className="part-filter" aria-label="Rulebook part filters"><button type="button" className={partAudienceFilter==='all'?'on':''} onClick={()=>setPartAudienceFilter('all')}>All</button>{PART_AUDIENCE_FILTERS.map(f=><button type="button" key={f.key} className={partAudienceFilter===f.category?'on':''} onClick={()=>setPartAudienceFilter(f.category)}>{f.label}</button>)}</div>}
      <div className="result-stack">{railResults.map(r=><button key={r.id} className={selected?.id===r.id?'hit active':'hit'} onClick={()=>choose(r)}><strong><NodeTitle node={r}/></strong></button>)}</div>
      <nav className="rail-utilities" aria-label="Workspace utilities">
        <details className="settings rail-settings">
          <summary aria-label="Graph settings" title="Graph settings">⚙</summary>
          <div className="settings-pop">
            <div className="settings-pop-heading"><strong>Graph settings</strong><span>Structure, filters and link visibility</span></div>
            <div className="settings-view-link"><button type="button" onClick={openIssuesLog}><span>Issues log</span><small>Review and maintain reported node issues</small></button></div>
            <div className="filter-section representation-section"><h4>Representation</h4><div className="type-grid representation-grid">{Object.entries(REPRESENTATIONS).map(([key,preset])=><button type="button" key={key} className={representation===key?'on':''} onClick={()=>applyRepresentation(key)}><span>{preset.label}</span></button>)}<button type="button" className={representation==='custom'?'on':''} onClick={()=>applyRepresentation('custom')}><span>Custom</span></button></div><p className="rep-hint"><b>{activeRep.label}</b>{activeRep.hint}</p></div>
            <label className="depth-control"><span>Graph depth</span><input type="range" min="1" max="3" step="1" value={depth} onInput={e=>{setDepth(Number(e.currentTarget.value));setRepresentation('custom')}} onChange={e=>{setDepth(Number(e.currentTarget.value));setRepresentation('custom')}}/><b>{depth}</b><span className="stepper"><button type="button" onClick={()=>{setDepth(d=>Math.max(1,d-1));setRepresentation('custom')}}>−</button><button type="button" onClick={()=>{setDepth(d=>Math.min(3,d+1));setRepresentation('custom')}}>＋</button></span></label>
            <label>Visible node cap <input type="number" min="30" max="800" value={limit} onChange={e=>setLimit(Number(e.target.value))}/></label>
            <label className="check"><input type="checkbox" checked={explicitOnly} onChange={e=>{setExplicitOnly(e.target.checked);setRepresentation('custom')}}/> Direct links only</label>
            <label className="check"><input type="checkbox" checked={showInsurance} onChange={e=>setShowInsurance(e.target.checked)}/> Insurance parts</label>
            <div className="filter-section"><h4>Link origin</h4><div className="type-grid origin-grid">{Object.entries(ORIGIN_FILTERS).map(([key,label])=><button type="button" key={key} className={originFilter===key?'on':''} onClick={()=>setOriginFilter(key)}><span>{label}</span></button>)}</div></div>
            <div className="filter-section"><h4>Material</h4><div className="type-grid material-grid">{MATERIAL_FILTERS.map(t=><button type="button" key={t} className={materialFilterOn(t,nodeTypes)?'on':''} onClick={()=>toggleNodeType(t)}><span>{materialLabel(t)}</span></button>)}</div></div>
            <div className="filter-section"><h4>Relationship edges</h4><div className="type-grid">{relationshipFilters.map(t=><button type="button" key={t} className={types.has(t)?'on':''} onClick={()=>toggleType(t)}><span>{relationLabel(t)}</span><em>{relationshipCount(t,stats,graph)}</em></button>)}</div></div>
          </div>
        </details>
        <a href="help.html" target="_blank" rel="noopener noreferrer" aria-label="Help" title="Help">?</a>
      </nav>
    </aside>

    <main className="canvas">
      {readingNode?<>
        <ProvisionReader rootNode={readingNode} api={api} onClose={()=>{setReadingNode(null);setIssueReportNode(null);setIssueText('');setError('');}} onReportIssue={n=>{setIssueReportNode(n);setIssueText('');setError('');}}/>
        {issueReportNode&&<IssueReportModal node={issueReportNode} text={issueText} setText={setIssueText} saving={issueSaving} saved={issueSaved} context="reading_mode" reportError={error} onClose={()=>{setIssueReportNode(null);setIssueText('');setError('');}} onSubmit={submitIssueReport}/>}
      </>:view==='reporting'?<ReportingGraphView onFeedback={n=>{setIssueReportNode(n);setIssueText('');}}/>:view==='issues'?<IssuesLogView onBack={()=>setView('graph')}/>:<>
        <div className="canvas-meta"><strong>{selected?.title||'Select a node'}</strong><span>{activeRep.label} · {visibleGraph.nodes.length} shown · {visibleGraph.edges.length} visible links · {Object.values(graph.available_edge_types||{}).reduce((a,b)=>a+b,0)} direct links available</span></div>
        <Graph graph={visibleGraph} selected={selected} detail={detail} nodeTypes={nodeTypes} relationshipTypes={types} relationshipFilters={relationshipFilters} availableEdgeTypes={graph.available_edge_types||{}} onToggleNodeType={toggleNodeType} onToggleRelationship={toggleType} onSelect={n=>{setDetail(n);setPanelOpen(true);}} onOpen={n=>choose(n,{drill:true})} onFeedback={n=>{setIssueReportNode(n);setIssueText('');}}/>
      </>}
    </main>

    <aside className={panelOpen?'inspector open':'inspector'}>
      <Explore node={detail} edges={selectedEdges} graph={graph} onChoose={choose} onRead={openReadingMode} onReportIssue={n=>{setIssueReportNode(n);setIssueText('');}}/>
    </aside>
    {issueReportNode&&!readingNode&&<IssueReportModal node={issueReportNode} text={issueText} setText={setIssueText} saving={issueSaving} saved={issueSaved} context="graph_view" onClose={()=>setIssueReportNode(null)} onSubmit={submitIssueReport}/>}
  </div>;
}

function IssuesLogView({onBack}){
  const [items,setItems]=useState([]);
  const [counts,setCounts]=useState(issueCounts([]));
  const [statusFilter,setStatusFilter]=useState('all');
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState('');
  const [editing,setEditing]=useState(null);
  const [deleting,setDeleting]=useState('');

  async function loadIssues(){
    setLoading(true);
    try{
      const data=await fetchJson(API_BASE+'/issues');
      const nextItems=data.items||[];
      setItems(nextItems);
      setCounts(issueCounts(nextItems));
      setError('');
    }catch(err){setError(err.message||String(err));}
    finally{setLoading(false);}
  }

  useEffect(()=>{loadIssues();},[]);

  const visibleItems=filterIssues(items,statusFilter);

  async function saveIssue(issueId,changes){
    setError('');
    const response=await fetch(API_BASE+`/issues/${encodeURIComponent(issueId)}`,{
      method:'PATCH',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(changes),
    });
    if(!response.ok) throw new Error(await responseErrorText(response));
    setEditing(null);
    await loadIssues();
  }

  async function removeIssue(issue){
    const title=issueNodeTitle(issue);
    if(!window.confirm(`Delete the issue reported for “${title}”?`)) return;
    setDeleting(issue.id); setError('');
    try{
      const response=await fetch(API_BASE+`/issues/${encodeURIComponent(issue.id)}`,{method:'DELETE'});
      if(!response.ok) throw new Error(await responseErrorText(response));
      await loadIssues();
    }catch(err){setError(err.message||String(err));}
    finally{setDeleting('');}
  }

  return <section className="issues-workspace" aria-labelledby="issues-log-title">
    <header className="issues-head">
      <div><span className="eyebrow">Workspace maintenance</span><h1 id="issues-log-title">Issues log</h1><p>Reported node issues from graph and reader views.</p></div>
      <button type="button" className="issues-back" onClick={onBack}>← Back to explorer</button>
    </header>
    <div className="issues-toolbar">
      <div className="issues-summary" aria-label="Issue counts">
        <span><strong>{counts.all}</strong> total</span>
        {ISSUE_STATUSES.map(key=><span key={key}><strong>{counts[key]}</strong> {issueStatusLabel(key).toLowerCase()}</span>)}
      </div>
      <label className="issues-filter"><span>Filter</span><select value={statusFilter} onChange={event=>setStatusFilter(event.target.value)} aria-label="Filter issues by status"><option value="all">All statuses</option>{ISSUE_STATUSES.map(key=><option key={key} value={key}>{issueStatusLabel(key)}</option>)}</select></label>
    </div>
    {error&&<div className="issues-error" role="alert">{error}</div>}
    {loading
      ? <div className="issues-state">Loading reported issues…</div>
      : error&&!items.length
        ? <div className="issues-state"><strong>Could not load reported issues</strong><span>Try opening the Issues log again. The API returned an error.</span></div>
      : !items.length
        ? <div className="issues-state"><strong>No reported issues</strong><span>Reports created from graph or reader views will appear here.</span></div>
        : !visibleItems.length
          ? <div className="issues-state"><strong>No issues match this filter</strong><span>Choose another status to see the remaining reports.</span></div>
          : <div className="issues-table-wrap"><table className="issues-table"><caption className="sr-only">Reported node issues</caption><thead><tr><th scope="col">Status</th><th scope="col">Reported</th><th scope="col">Node</th><th scope="col">Issue</th><th scope="col">Actions</th></tr></thead><tbody>{visibleItems.map(issue=><tr key={issue.id}>
            <td><span className={`issue-status status-${issue.status||'unknown'}`}>{issueStatusLabel(issue.status)}</span></td>
            <td className="issue-date"><time dateTime={issue.created_at||undefined}>{issueDateLabel(issue.created_at)}</time></td>
            <td className="issue-node"><strong>{issueNodeTitle(issue)}</strong><span>{nodeTypeLabel(issue.node?.node_type)}</span>{issue.node?.url&&<a href={issue.node.url} target="_blank" rel="noopener noreferrer">Source ↗</a>}</td>
            <td className="issue-copy"><span>{issue.description||'No description provided.'}</span><small>{issueContextLabel(issue.context)}</small></td>
            <td className="issue-actions"><button type="button" onClick={()=>setEditing(issue)}>Edit</button><button type="button" className="issue-delete" disabled={deleting===issue.id} onClick={()=>removeIssue(issue)}>{deleting===issue.id?'Deleting…':'Delete'}</button></td>
          </tr>)}</tbody></table></div>}
    {editing&&<IssueEditModal issue={editing} onClose={()=>setEditing(null)} onSave={saveIssue}/>}
  </section>;
}

function IssueEditModal({issue,onClose,onSave}){
  const [description,setDescription]=useState(issue.description||'');
  const [status,setStatus]=useState(issue.status||'open');
  const [saving,setSaving]=useState(false);
  const [error,setError]=useState('');

  async function submit(event){
    event.preventDefault(); setSaving(true); setError('');
    try{ await onSave(issue.id,{description,status}); }
    catch(err){setError(err.message||String(err));}
    finally{setSaving(false);}
  }

  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Amend reported issue" onClick={event=>{if(event.target===event.currentTarget)onClose();}}>
    <form className="node-feedback-modal issue-edit-modal" onSubmit={submit}>
      <div className="modal-head"><div><span className="eyebrow">Issue maintenance</span><h3>Amend reported issue</h3></div><button type="button" onClick={onClose} aria-label="Close">×</button></div>
      <div className="feedback-node-summary"><span>{nodeTypeLabel(issue.node?.node_type)}</span><strong>{issueNodeTitle(issue)}</strong></div>
      <label className="feedback-editor"><span>Description</span><textarea value={description} maxLength={2000} onChange={event=>setDescription(event.target.value)} autoFocus/></label>
      <label className="issue-status-editor"><span>Status</span><select value={status} onChange={event=>setStatus(event.target.value)}>{ISSUE_STATUSES.map(key=><option key={key} value={key}>{issueStatusLabel(key)}</option>)}</select></label>
      {error&&<p className="issues-error" role="alert">{error}</p>}
      <div className="modal-actions"><button type="button" onClick={onClose}>Cancel</button><button type="submit" disabled={saving}>{saving?'Saving…':'Save changes'}</button></div>
    </form>
  </div>;
}

function issueNodeTitle(issue){
  return issue?.node?.title||issue?.node?.stable_key||issue?.node?.id||'Unknown node';
}
function nodeTypeLabel(value){
  return String(value||'Unknown node').replaceAll('_',' ');
}
function issueContextLabel(value){
  return value==='reading_mode'?'Reader view':value==='graph_view'?'Graph view':value||'Unknown context';
}

function IssueReportModal({node,text,setText,saving,saved,context,reportError='',onClose,onSubmit}){
  const readingIssue=context==='reading_mode';
  const [minimised,setMinimised]=useState(false);
  const reportForm=<form className={`node-feedback-modal issue-report-modal ${readingIssue?'reading-issue-report-modal':''}`} onSubmit={e=>{e.preventDefault();onSubmit();}}>
    <div className="modal-head"><div><span className="eyebrow">Report an issue</span><h3>Report an issue with this node</h3></div><div className="issue-report-head-actions">{readingIssue&&<button type="button" className="issue-report-minimise" aria-label={minimised?'Expand description':'Minimise description'} onClick={()=>setMinimised(value=>!value)} aria-expanded={!minimised} aria-controls={readingIssue?'reading-issue-description':undefined}>{minimised?'Expand description':'Minimise description'}</button>}<button type="button" onClick={onClose} aria-label="Close">×</button></div></div>
    <div id={readingIssue?'reading-issue-description':undefined} className={`issue-report-body ${readingIssue&&minimised?'is-minimised':''}`}>
      <div className="feedback-node-summary"><span>{label(node.node_type)}</span><strong>{displayNodeTitle(node)}</strong>{node.url&&<a href={node.url} target="_blank" rel="noopener noreferrer">Open source ↗</a>}</div>
      <label className={`feedback-editor ${readingIssue?'issue-report-description':''}`}>Describe the issue (optional)<textarea value={text} onChange={e=>setText(e.target.value)} placeholder="Example: this node should link to SS3/18, but the reference is missing." autoFocus/></label>
      <p className="muted issue-context-note">{context==='reading_mode'?'Reported from reading mode.':'Reported from graph view.'}</p>
    </div>
    {reportError&&<p className="issue-report-error" role="alert">{reportError}</p>}
    <div className="modal-actions">
      <button type="button" onClick={onClose}>Cancel</button>
      <button type="submit" disabled={saving||saved} className={saved?'issue-saved':''}>{saved?'✓ Reported':saving?'Saving…':'Submit report'}</button>
    </div>
  </form>;
  return readingIssue
    ? <div className="reading-issue-layer" role="dialog" aria-modal="false" aria-label="Report an issue with this node">{reportForm}</div>
    : <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Report an issue with this node" onClick={e=>{if(e.target===e.currentTarget)onClose();}}>{reportForm}</div>;
}
function ProvisionReader({rootNode,api,onClose,onReportIssue}){
  const [root,setRoot]=useState(rootNode);
  const [referenceDepth,setReferenceDepth]=useState(1);
  const [contents,setContents]=useState({root:null,children:[]});
  const [referenceGraph,setReferenceGraph]=useState({nodes:[rootNode],edges:[]});
  const [loading,setLoading]=useState(true);
  const [loadError,setLoadError]=useState('');
  const [expandedId,setExpandedId]=useState('');
  const [pinned,setPinned]=useState([]);
  const [activePinnedId,setActivePinnedId]=useState('');
  const [mobileShelfOpen,setMobileShelfOpen]=useState(false);
  const [returnInlinePath,setReturnInlinePath]=useState([]);
  const readingScrollRef=useRef(null);

  useEffect(()=>{
    setPinned([]);
    setActivePinnedId('');
    setMobileShelfOpen(false);
  },[rootNode.id]);

  useEffect(()=>{
    let cancelled=false;
    setRoot(rootNode);
    setContents({root:null,children:[]});
    setReferenceGraph({nodes:[rootNode],edges:[]});
    setExpandedId('');
    setReturnInlinePath([]);
    setLoading(true);
    setLoadError('');
    api(`/node/${encodeURIComponent(rootNode.id)}/reader?reference_depth=${referenceDepth}`).then(bundle=>{
      if(cancelled) return;
      setRoot(bundle.contents?.root||rootNode);
      setContents(bundle.contents||{root:rootNode,children:[]});
      setReferenceGraph(bundle.graph||{nodes:[rootNode],edges:[]});
    }).catch(error=>{
      if(!cancelled) setLoadError(error.message||String(error));
    }).finally(()=>{
      if(!cancelled) setLoading(false);
    });
    return ()=>{cancelled=true;};
  },[rootNode.id,referenceDepth]);

  const referenceEdgesBySource=useMemo(()=>{
    const bySource=new Map();
    for(const edge of referenceGraph.edges||[]){
      const sourceIds=new Set([
        edge.from_node_id,
        ...(edge.metadata?.rolled_up_from_from_node_ids||[]),
        ...(edge.metadata?.reference_occurrences||[]).map(item=>item.source_node_id),
      ].filter(Boolean));
      for(const sourceId of sourceIds){
        bySource.set(sourceId,[...(bySource.get(sourceId)||[]),edge]);
      }
    }
    return bySource;
  },[referenceGraph]);
  const sections=useMemo(()=>readingSpine(contents).map(entry=>{
    const blocks=entry.sourceBlocks?.length
      ?entry.sourceBlocks.map(b=>({kind:b.marker?'list-item':'prose',marker:b.marker||'',depth:b.depth||0,text:b.text}))
      :legalTextBlocks(entry.bodyText||'');
    const references=blocks.length?mergeOverlappingReferences(assignReferencesToParagraphs(
      blocks,
      readerReferences(entry.node,{
        nodes:referenceGraph.nodes||[],
        edges:referenceEdgesBySource.get(entry.node.id)||[],
      }),
    )).map(reference=>({
      ...reference,
      readingSourceId:entry.node.id,
      readingSourceTitle:entry.node.title||displayNodeTitle(entry.node),
      readingBlockId:reference.paragraphIndex>=0
        ?`${entry.node.id}:${reference.paragraphIndex}`
        :`${entry.node.id}:references`,
      rootReferenceId:reference.id,
      inlinePath:[reference.id],
    })):[];
    return {...entry,blocks,references};
  }),[contents,referenceGraph.nodes,referenceEdgesBySource]);
  const references=useMemo(
    ()=>sections.flatMap(section=>section.references),
    [sections],
  );
  const referenceById=useMemo(
    ()=>new Map(references.map(reference=>[reference.id,reference])),
    [references],
  );
  const placedByBlock=useMemo(()=>{
    const placed=new Map();
    for(const reference of references){
      if(reference.paragraphIndex<0) continue;
      placed.set(
        reference.readingBlockId,
        [...(placed.get(reference.readingBlockId)||[]),reference],
      );
    }
    return placed;
  },[references]);
  const linkedProvisionCount=references.reduce(
    (total,reference)=>total+Math.max(1,reference.members?.length||0),
    0,
  );
  const pinnedReferences=pinned.map(reference=>referenceById.get(reference.id)||reference);
  const pinnedIds=useMemo(
    ()=>new Set(pinnedReferences.map(reference=>reference.id)),
    [pinnedReferences],
  );

  useEffect(()=>{
    const scroller=readingScrollRef.current;
    if(!scroller||!pinnedReferences.length) return;
    function updateActivePin(){
      const viewport=scroller.getBoundingClientRect();
      const readingFocus=viewport.top+viewport.height*.45;
      const rows=[...scroller.querySelectorAll('[data-reference-ids]')]
        .sort((a,b)=>{
          const aBox=a.getBoundingClientRect();
          const bBox=b.getBoundingClientRect();
          const aCentre=aBox.top+aBox.height/2;
          const bCentre=bBox.top+bBox.height/2;
          return Math.abs(aCentre-readingFocus)-Math.abs(bCentre-readingFocus);
        });
      for(const row of rows){
        const ids=(row.dataset.referenceIds||'').split(',').filter(Boolean);
        const match=ids.find(id=>pinnedIds.has(id))
          ||pinnedReferences.find(reference=>reference.readingBlockId===row.dataset.readingBlockId)?.id;
        if(match){
          setActivePinnedId(match);
          return;
        }
      }
    }
    updateActivePin();
    scroller.addEventListener('scroll',updateActivePin,{passive:true});
    return ()=>scroller.removeEventListener('scroll',updateActivePin);
  },[[...pinnedIds].join(',')]);

  function activateReference(reference){
    if(pinnedIds.has(reference.id)){
      setActivePinnedId(reference.id);
      setMobileShelfOpen(true);
      return;
    }
    setReturnInlinePath([]);
    setExpandedId(current=>current===reference.id?'':reference.id);
  }
  function pinReference(reference){
    setPinned(current=>current.some(item=>item.id===reference.id)?current:[...current,reference]);
    setActivePinnedId(reference.id);
  }
  function returnInline(reference){
    setPinned(current=>current.filter(item=>item.id!==reference.id));
    setExpandedId(reference.rootReferenceId||reference.id);
    setReturnInlinePath(reference.inlinePath||[reference.id]);
    setMobileShelfOpen(false);
    requestAnimationFrame(()=>{
      readingScrollRef.current
        ?.querySelector(`[data-reading-block-id="${reference.readingBlockId}"]`)
        ?.scrollIntoView({behavior:'smooth',block:'center'});
    });
  }
  function removePin(reference){
    setPinned(current=>current.filter(item=>item.id!==reference.id));
    setActivePinnedId(current=>current===reference.id?'':current);
  }

  const sourceHeading=root?.metadata?.part_title
    ||root?.metadata?.document_title
    ||root?.metadata?.source_title
    ||label(root?.node_type);
  const provisionCount=sections.filter(section=>section.blocks.length).length;
  const hasBody=sections.some(section=>section.blocks.length);
  return <div className="provision-reader">
    <header className="provision-reader-header">
      <button type="button" className="provision-reader-back" onClick={onClose}>← Graph</button>
      <div><span>Reading mode</span><strong>{displayNodeTitle(root)}</strong></div>
      <div className="reader-header-actions">
        <button type="button" className="report-issue-btn reader-report-btn" onClick={()=>onReportIssue?.(root)}>⚑ Report an issue with this node</button>
        <button type="button" className="reference-shelf-toggle" onClick={()=>setMobileShelfOpen(true)}>
          Pinned references <b>{pinnedReferences.length}</b>
        </button>
      </div>
    </header>
    <div className="provision-reader-layout">
      <main className="provision-reading-scroll" ref={readingScrollRef}>
        <article className="provision-reading-spine">
          <header className="provision-title-block">
            <span className="provision-kicker">Reading spine · {sourceHeading}</span>
            <h1>{displayNodeTitle(root)}</h1>
            <div className="provision-byline">
              <span>{loading
                ?'Loading contained provisions and citations…'
                :`${linkedProvisionCount} linked provision${linkedProvisionCount===1?'':'s'} across ${references.length} citation${references.length===1?'':'s'} · ${provisionCount} source provision${provisionCount===1?'':'s'} · reference depth ${referenceDepth}`}</span>
              {root?.url&&<a href={root.url} target="_blank" rel="noopener noreferrer">Open original source ↗</a>}
            </div>
            <div className="reference-depth-control" role="group" aria-label="Reference depth">
              <span>Reference depth</span>
              {[1,2,3].map(depth=><button
                type="button"
                key={depth}
                aria-pressed={referenceDepth===depth}
                onClick={()=>setReferenceDepth(depth)}
                disabled={loading}
              >{depth}</button>)}
              <small>{referenceDepth===1?'Direct references only':`Show references up to ${referenceDepth} levels deep`}</small>
            </div>
          </header>
          {loadError&&<p className="reader-load-error">Reader content could not be loaded: {loadError}</p>}
          {!hasBody&&!loading&&<p className="reader-empty">This node and its contained provisions have no body text. The original source remains available above.</p>}
          <div className="legal-paragraphs">
            {sections.map(section=>{
              const unmatched=section.references.filter(reference=>reference.paragraphIndex<0);
              return <section
                className={`reading-provision-section ${section.isRoot?'is-root':'is-child'}`}
                key={section.node.id}
                style={{'--section-depth':Math.max(0,section.depth-1)}}
              >
                {!section.isRoot&&<header className="reading-provision-heading">
                  <span>{label(section.node.node_type)}</span>
                  <h2>{section.node.title||displayNodeTitle(section.node)}</h2>
                  {section.node.url&&<a href={section.node.url} target="_blank" rel="noopener noreferrer">Source ↗</a>}
                </header>}
                {section.blocks.map((block,index)=>{
                  const blockId=`${section.node.id}:${index}`;
                  const blockReferences=placedByBlock.get(blockId)||[];
                  const segments=paragraphCitationSegments(block,blockReferences);
                  const expanded=blockReferences.find(reference=>reference.id===expandedId);
                  return <section
                    className={`legal-paragraph legal-block-${block.kind} legal-depth-${Math.min(block.depth||0,3)}`}
                    key={blockId}
                    data-reading-block-id={blockId}
                    data-reference-ids={blockReferences.map(reference=>reference.id).join(',')}
                  >
                    <span className="legal-paragraph-number" aria-hidden={!block.marker}>
                      {block.marker||''}
                    </span>
                    <p>{segments.map((segment,segmentIndex)=>segment.type==='citation'
                      ?<button
                        type="button"
                        key={`${segment.reference.id}-${segmentIndex}`}
                        className={`legal-citation ${pinnedIds.has(segment.reference.id)?'is-pinned':''}`}
                        onClick={()=>activateReference(segment.reference)}
                        aria-expanded={expandedId===segment.reference.id}
                      >{segment.text}<sup>{segment.reference.relationship.code}</sup></button>
                      :<React.Fragment key={segmentIndex}>{segment.text}</React.Fragment>)}</p>
                    {expanded&&<InlineLegalReference
                      reference={expanded}
                      onCollapse={()=>setExpandedId('')}
                      onPin={pinReference}
                      referenceGraph={referenceGraph}
                      referenceEdgesBySource={referenceEdgesBySource}
                      maxDepth={referenceDepth}
                      requestedPath={returnInlinePath}
                      pinnedIds={pinnedIds}
                      onPinnedActivate={activateReference}
                    />}
                  </section>;
                })}
                {unmatched.length>0&&<section
                  className="legal-paragraph reader-reference-index"
                  data-reading-block-id={`${section.node.id}:references`}
                  data-reference-ids={unmatched.map(reference=>reference.id).join(',')}
                >
                  <span className="legal-paragraph-number">REF</span>
                  <div>
                    <h2>Other links from {section.node.title||displayNodeTitle(section.node)}</h2>
                    <p>These relationships are recorded for this provision but are not anchored to a unique phrase in its text.</p>
                    <div className="reader-reference-links">{unmatched.map(reference=><button type="button" key={reference.id} onClick={()=>activateReference(reference)}><b>{reference.relationship.code}</b>{referenceDisplayTitle(reference)}</button>)}</div>
                    {unmatched.find(reference=>reference.id===expandedId)&&<InlineLegalReference
                      reference={unmatched.find(reference=>reference.id===expandedId)}
                      onCollapse={()=>setExpandedId('')}
                      onPin={pinReference}
                      referenceGraph={referenceGraph}
                      referenceEdgesBySource={referenceEdgesBySource}
                      maxDepth={referenceDepth}
                      requestedPath={returnInlinePath}
                      pinnedIds={pinnedIds}
                      onPinnedActivate={activateReference}
                    />}
                  </div>
                </section>}
              </section>;
            })}
          </div>
          {loading&&<div className="reader-loading">Loading provision hierarchy and direct references…</div>}
        </article>
      </main>
      {mobileShelfOpen&&<button type="button" className="reference-shelf-backdrop" onClick={()=>setMobileShelfOpen(false)} aria-label="Close pinned references"/>}
      <ReferenceShelf
        references={pinnedReferences}
        activeId={activePinnedId}
        mobileOpen={mobileShelfOpen}
        onMobileClose={()=>setMobileShelfOpen(false)}
        onActivate={setActivePinnedId}
        onReturnInline={returnInline}
        onRemove={removePin}
      />
    </div>
  </div>;
}

function InlineLegalReference({
  reference,
  onCollapse,
  onPin,
  referenceGraph,
  referenceEdgesBySource,
  maxDepth=1,
  level=1,
  requestedPath=[],
  pinnedIds=new Set(),
  onPinnedActivate=()=>{},
}){
  const members=reference.members?.length
    ?reference.members
    :[{id:reference.id,node:reference.node,edge:reference.edge,citation:reference.citation}];
  const [selectedMemberId,setSelectedMemberId]=useState(members[0]?.id||'');
  const [expandedNestedId,setExpandedNestedId]=useState('');
  useEffect(()=>setSelectedMemberId(members[0]?.id||''),[reference.id]);
  const selectedMember=members.find(member=>member.id===selectedMemberId)||members[0];
  const selectedNode=selectedMember?.node||reference.node;
  const selectedEdge=selectedMember?.edge||reference.edge;
  const blocks=useMemo(()=>legalTextBlocks(selectedNode?.text||''),[selectedNode?.id,selectedNode?.text]);
  const nestedReferences=useMemo(()=>level<maxDepth?mergeOverlappingReferences(assignReferencesToParagraphs(
    blocks,
    readerReferences(selectedNode,{
      nodes:referenceGraph?.nodes||[],
      edges:referenceEdgesBySource?.get(selectedNode?.id)||[],
    }),
  )).map(nestedReference=>({
    ...nestedReference,
    readingBlockId:reference.readingBlockId,
    rootReferenceId:reference.rootReferenceId||reference.id,
    inlinePath:[...(reference.inlinePath||[reference.id]),nestedReference.id],
  })):[],[
    blocks,
    level,
    maxDepth,
    reference.id,
    reference.readingBlockId,
    reference.rootReferenceId,
    reference.inlinePath,
    referenceGraph?.nodes,
    referenceEdgesBySource,
    selectedNode?.id,
  ]);
  const nestedByBlock=useMemo(()=>{
    const placed=new Map();
    for(const nestedReference of nestedReferences){
      if(nestedReference.paragraphIndex<0) continue;
      placed.set(
        nestedReference.paragraphIndex,
        [...(placed.get(nestedReference.paragraphIndex)||[]),nestedReference],
      );
    }
    return placed;
  },[nestedReferences]);
  const unmatchedNested=nestedReferences.filter(item=>item.paragraphIndex<0);
  useEffect(()=>setExpandedNestedId(''),[reference.id,selectedNode?.id,maxDepth]);
  useEffect(()=>{
    const currentIndex=requestedPath.indexOf(reference.id);
    const nextId=currentIndex>=0?requestedPath[currentIndex+1]:'';
    if(nextId&&nestedReferences.some(item=>item.id===nextId)) setExpandedNestedId(nextId);
  },[reference.id,requestedPath.join('|'),nestedReferences.map(item=>item.id).join('|')]);
  const sourceUrl=selectedNode?.url||selectedEdge?.source_url;
  const applicabilityNote=selectedNode?.metadata?.applicability_note;
  const relatedProvisions=selectedNode?.metadata?.related_provisions||[];
  function activateNestedReference(nestedReference){
    if(pinnedIds.has(nestedReference.id)){
      onPinnedActivate(nestedReference);
      return;
    }
    setExpandedNestedId(current=>current===nestedReference.id?'':nestedReference.id);
  }
  return <aside className={`inline-legal-reference relationship-${reference.relationship.code.toLowerCase()}`}>
    <header>
      <div><span>{reference.relationship.code} · {reference.relationship.label}</span><small>{reference.sourceHeading}</small></div>
      <button type="button" onClick={onCollapse} aria-label="Collapse reference">Collapse ↑</button>
    </header>
    <h2>{referenceDisplayTitle(reference)}</h2>
    {members.length>1&&<div className="inline-reference-members" aria-label="Provisions in this citation">
      {members.map((member,index)=><button
        type="button"
        key={member.id}
        className={member.id===selectedMember?.id?'is-selected':''}
        onClick={()=>setSelectedMemberId(member.id)}
      ><span>{String(index+1).padStart(2,'0')}</span>{member.relationship&&<b>{member.relationship.code}</b>}{member.citation||displayNodeTitle(member.node)}</button>)}
    </div>}
    {members.length>1&&<h3 className="inline-reference-selected-title">{displayNodeTitle(selectedNode)}</h3>}
    <div className="inline-reference-text">{blocks.length?blocks.map((block,index)=>{
      const blockReferences=nestedByBlock.get(index)||[];
      const segments=paragraphCitationSegments(block,blockReferences);
      const expandedNested=blockReferences.find(item=>item.id===expandedNestedId);
      return <div
        className={`legal-text-block legal-text-block-${block.kind} legal-depth-${Math.min(block.depth||0,3)}`}
        key={`${index}-${block.marker}-${block.text.slice(0,24)}`}
      >
        <span aria-hidden={!block.marker}>{block.marker||''}</span>
        <p>{segments.map((segment,segmentIndex)=>segment.type==='citation'
          ?<button
            type="button"
            className={`legal-citation ${pinnedIds.has(segment.reference.id)?'is-pinned':''}`}
            key={`${segment.reference.id}-${segmentIndex}`}
            onClick={()=>activateNestedReference(segment.reference)}
            aria-expanded={expandedNestedId===segment.reference.id}
          >{segment.text}<sup>{segment.reference.relationship.code}</sup></button>
          :<React.Fragment key={segmentIndex}>{segment.text}</React.Fragment>)}</p>
        {expandedNested&&<InlineLegalReference
          reference={expandedNested}
          onCollapse={()=>setExpandedNestedId('')}
          onPin={onPin}
          referenceGraph={referenceGraph}
          referenceEdgesBySource={referenceEdgesBySource}
          maxDepth={maxDepth}
          level={level+1}
          requestedPath={requestedPath}
          pinnedIds={pinnedIds}
          onPinnedActivate={onPinnedActivate}
        />}
      </div>;
    }):<p>No body text is available for this reference.</p>}
      {unmatchedNested.length>0&&<div className="inline-reference-other-links">
        <strong>Other links</strong>
        {unmatchedNested.map(item=><button
          type="button"
          key={item.id}
          className={pinnedIds.has(item.id)?'is-pinned':''}
          onClick={()=>activateNestedReference(item)}
        >{item.relationship.code} · {referenceDisplayTitle(item)}</button>)}
        {unmatchedNested.find(item=>item.id===expandedNestedId)&&<InlineLegalReference
          reference={unmatchedNested.find(item=>item.id===expandedNestedId)}
          onCollapse={()=>setExpandedNestedId('')}
          onPin={onPin}
          referenceGraph={referenceGraph}
          referenceEdgesBySource={referenceEdgesBySource}
          maxDepth={maxDepth}
          level={level+1}
          requestedPath={requestedPath}
          pinnedIds={pinnedIds}
          onPinnedActivate={onPinnedActivate}
        />}
      </div>}
    </div>
    {applicabilityNote&&<aside className="inline-reference-applicability">
      <strong>UK applicability</strong>
      <p>{applicabilityNote}</p>
      {relatedProvisions.length>0&&<ul>{relatedProvisions.map(item=><li key={`${item.category}-${item.citation}`}><a href={item.url} target="_blank" rel="noopener noreferrer">{item.category}</a><span>{item.citation}</span></li>)}</ul>}
    </aside>}
    <footer>
      {sourceUrl&&<a href={sourceUrl} target="_blank" rel="noopener noreferrer">Open original source ↗</a>}
      <button type="button" onClick={()=>{onPin(reference);onCollapse();}}>Pin to shelf</button>
    </footer>
  </aside>;
}

function ReferenceShelf({references,activeId,mobileOpen,onMobileClose,onActivate,onReturnInline,onRemove}){
  const bodyRef=useRef(null);
  const measurementRef=useRef(null);
  const [availableHeight,setAvailableHeight]=useState(640);
  const [measuredHeights,setMeasuredHeights]=useState({});
  const [temporaryId,setTemporaryId]=useState('');
  const shelfItems=references.map(reference=>{
    const firstMember=reference.members?.[0];
    const sourceNode=firstMember?.node||reference.node;
    const sourceEdge=firstMember?.edge||reference.edge;
    return {
      reference,
      sourceNode,
      sourceUrl:sourceNode?.url||sourceEdge?.source_url,
    };
  });
  useLayoutEffect(()=>{
    const body=bodyRef.current;
    const measurements=measurementRef.current;
    if(!body||!measurements) return;
    const update=()=>{
      setAvailableHeight(body.getBoundingClientRect().height);
      const next={};
      measurements.querySelectorAll('[data-shelf-density]').forEach(element=>{
        next[element.dataset.shelfDensity]=element.getBoundingClientRect().height;
      });
      setMeasuredHeights(current=>Object.keys(next).every(key=>Math.abs((current[key]||0)-next[key])<.5)
        &&Object.keys(current).length===Object.keys(next).length?current:next);
    };
    update();
    if(typeof ResizeObserver==='undefined') return;
    const observer=new ResizeObserver(update);
    observer.observe(body);
    measurements.querySelectorAll('[data-shelf-density]').forEach(element=>observer.observe(element));
    return ()=>observer.disconnect();
  },[references]);
  const density=references.length?referenceShelfDensity(availableHeight,measuredHeights):'full';
  useEffect(()=>{if(density!=='summary') setTemporaryId('');},[density]);
  return <aside className={`reference-shelf density-${density} ${mobileOpen?'is-mobile-open':''}`} aria-label="Pinned references">
    <header>
      <div><span>Reference shelf</span><strong>{references.length} pinned</strong></div>
      <button type="button" onClick={onMobileClose} aria-label="Close pinned references">×</button>
    </header>
    <div className="reference-shelf-list" ref={bodyRef}>
      {!references.length&&<div className="reference-shelf-empty"><span>PIN</span><strong>Keep provisions in view</strong><p>Pin an expanded reference and it will remain here while the main reading spine stays fixed.</p></div>}
      {shelfItems.map(({reference,sourceNode,sourceUrl},index)=>{
        const temporarilyExpanded=density==='summary'&&temporaryId===reference.id;
        return <article
          key={reference.id}
          className={`reference-shelf-card ${activeId===reference.id?'is-current':''} ${temporarilyExpanded?'is-temporarily-expanded':''}`}
          aria-current={activeId===reference.id?'location':undefined}
          onClick={()=>{
            onActivate(reference.id);
            if(density==='summary') setTemporaryId(current=>current===reference.id?'':reference.id);
          }}
        >
          <header><span>{String(index+1).padStart(2,'0')}</span><b>{reference.relationship.code}{reference.members?.length>1?` · ${reference.members.length}`:''}</b><button type="button" onClick={event=>{event.stopPropagation();onRemove(reference);}} aria-label={`Remove ${referenceDisplayTitle(reference)}`}>×</button></header>
          <h3>{referenceDisplayTitle(reference)}</h3>
          <p>{sourceNode?.text||'No excerpt available.'}</p>
          <footer>
            <button type="button" onClick={event=>{event.stopPropagation();onReturnInline(reference);}}>Return inline</button>
            {sourceUrl&&<a href={sourceUrl} target="_blank" rel="noopener noreferrer" onClick={event=>event.stopPropagation()}>Source ↗</a>}
          </footer>
        </article>;
      })}
    </div>
    <div className="reference-shelf-measurements" ref={measurementRef} aria-hidden="true">
      {['full','compact','dense','summary'].map(measurementDensity=><div
        className={`reference-shelf-measurement-list density-${measurementDensity}`}
        data-shelf-density={measurementDensity}
        key={measurementDensity}
      >
        {shelfItems.map(({reference,sourceNode,sourceUrl},index)=><article className="reference-shelf-card" key={reference.id}>
          <header><span>{String(index+1).padStart(2,'0')}</span><b>{reference.relationship.code}{reference.members?.length>1?` · ${reference.members.length}`:''}</b><i>×</i></header>
          <h3>{referenceDisplayTitle(reference)}</h3>
          <p>{sourceNode?.text||'No excerpt available.'}</p>
          <footer><span>Return inline</span>{sourceUrl&&<span>Source ↗</span>}</footer>
        </article>)}
      </div>)}
    </div>
  </aside>;
}

function ReportingGraphView({onFeedback}){
  const [query,setQuery]=useState('');
  const [estate,setEstate]=useState('supervisory_reporting');
  const [includeHistoric,setIncludeHistoric]=useState(false);
  const [catalog,setCatalog]=useState({returns:[],technical_artifacts:[],counts:{}});
  const [selectedId,setSelectedId]=useState('');
  const [catalogDetail,setCatalogDetail]=useState(null);
  const [graph,setGraph]=useState({nodes:[],edges:[],available_edge_types:{}});
  const [nodeDetail,setNodeDetail]=useState(null);
  const [reportingSurface,setReportingSurface]=useState('graph');
  const [cellData,setCellData]=useState(null);
  const [cellQuery,setCellQuery]=useState('');
  const [cellTemplate,setCellTemplate]=useState('');
  const [selectedCell,setSelectedCell]=useState(null);
  const cellLoadId=useRef(0);
  const [impactQuery,setImpactQuery]=useState('');
  const [impactResults,setImpactResults]=useState([]);
  const [impactTarget,setImpactTarget]=useState(null);
  const [impactData,setImpactData]=useState(null);
  const [catalogOpen,setCatalogOpen]=useState(true);
  const [edgeGroups,setEdgeGroups]=useState(new Set(REPORTING_OVERVIEW_EDGE_GROUP_KEYS));
  const [nodeTypes,setNodeTypes]=useState(new Set(REPORTING_NODE_TYPES.filter(t=>!['DataPoint','TemplateRow','TemplateColumn'].includes(t))));
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  useEffect(()=>{ loadCatalog(); },[estate,includeHistoric]);
  const visibleEdgeTypes=useMemo(()=>reportingEdgeTypesForGroups(edgeGroups),[edgeGroups]);
  const edgeGroupCounts=useMemo(()=>reportingEdgeGroupCounts(graph),[graph]);
  const activeGraph=useMemo(()=>{
    if(!selectedId){
      return nodeDetail?.node_type==='ReportingCollection'
        ? reportingOneHopGraph(graph,nodeDetail.id)
        : graph;
    }
    const filtered=filterGraph(graph,nodeTypes,visibleEdgeTypes,'all',nodeDetail?.id,true);
    return reportingOneHopGraph(filtered,nodeDetail?.id);
  },[graph,nodeTypes,visibleEdgeTypes,nodeDetail?.id,selectedId]);
  const graphRoot=useMemo(()=>graph.nodes?.find(n=>['RequirementEdition','DataItem','ReportingReturn','DisclosureSet'].includes(n.node_type))||null,[graph]);
  const selectedEdges=useMemo(()=>activeGraph.edges.filter(edge=>nodeDetail&&(edge.from_node_id===nodeDetail.id||edge.to_node_id===nodeDetail.id)),[activeGraph,nodeDetail]);
  const childGroups=useMemo(()=>reportingChildGroups(nodeDetail,activeGraph),[activeGraph,nodeDetail]);
  const parentNodes=useMemo(()=>reportingParentNodes(nodeDetail,activeGraph),[activeGraph,nodeDetail]);

  async function loadCatalog(search=query){
    setBusy(true); setError('');
    try{
      const p=new URLSearchParams({estate,include_historic:String(includeHistoric)});
      if(search.trim()) p.set('q',search.trim());
      const data=await fetchJson(API_BASE+`/reporting/catalog?${p}`);
      setCatalog(data);
      if(selectedId && !data.returns.some(row=>row.return_id===selectedId)){ setSelectedId(''); setCatalogDetail(null); setNodeDetail(null); setReportingSurface('graph'); setCellData(null); setSelectedCell(null); setCatalogOpen(true); }
      if(!selectedId){
        const overview=reportingCatalogOverviewGraph(data.returns||[]);
        setGraph(overview);
        setNodeDetail(overview.nodes.find(node=>node.node_type==='ReportingCollection')||overview.nodes[0]||null);
      }
    }catch(err){ setError(err.message||String(err)); }
    finally{ setBusy(false); }
  }
  async function openReturn(row){
    setSelectedId(row.return_id); setBusy(true); setError('');
    setEdgeGroups(new Set(REPORTING_REQUIREMENT_EDGE_GROUP_KEYS));
    setReportingSurface('graph'); setCellData(null); setCellQuery(''); setCellTemplate(''); setSelectedCell(null);
    setCatalogOpen(false);
    try{
      const graphKey=row.edition_id||row.return_id;
      const [detailData,graphData]=await Promise.all([
        fetchJson(API_BASE+`/reporting/catalog/${encodeURIComponent(row.return_id)}`),
        fetchJson(API_BASE+`/reporting/graph/overview?selected_return=${encodeURIComponent(graphKey)}&limit=1&child_limit=360`),
      ]);
      setCatalogDetail(detailData); setGraph(graphData);
      setNodeDetail(graphData.nodes?.find(n=>['RequirementEdition','DataItem','ReportingReturn','DisclosureSet'].includes(n.node_type))||graphData.nodes?.[0]||null);
    }
    catch(err){ setError(err.message||String(err)); }
    finally{ setBusy(false); }
  }
  async function loadCells({template=cellTemplate,preferredNode=nodeDetail}={}){
    if(!selectedId) return;
    const loadId=++cellLoadId.current;
    setBusy(true); setError('');
    try{
      let selectedTemplate=template;
      if(!selectedTemplate){
        const summary=await fetchJson(API_BASE+`/reporting/catalog/${encodeURIComponent(selectedId)}/cells?limit=1&offset=0`);
        const graphTemplate=reportingTemplateForNode(preferredNode,summary.templates);
        if(reportingNodeSelectsTemplate(preferredNode)&&!graphTemplate){
          if(loadId!==cellLoadId.current) return;
          setCellTemplate('');
          setCellData({...summary,cells:[],selected_template_id:'',coverage:'selected_template_unavailable',requested_template:{title:preferredNode.title||preferredNode.metadata?.name||'Selected template'}});
          setSelectedCell(null);
          return;
        }
        selectedTemplate=graphTemplate?.template_id||summary.templates?.[0]?.template_id||'';
        if(loadId!==cellLoadId.current) return;
        setCellTemplate(selectedTemplate);
        if(!selectedTemplate){
          setCellData(summary);
          setSelectedCell(null);
          return;
        }
      }
      const pageSize=500;
      const layoutPromise=fetchJson(API_BASE+`/reporting/templates/${encodeURIComponent(selectedTemplate)}/layout`).catch(()=>null);
      const firstParams=new URLSearchParams({limit:String(pageSize),offset:'0',template_id:selectedTemplate});
      const first=await fetchJson(API_BASE+`/reporting/catalog/${encodeURIComponent(selectedId)}/cells?${firstParams}`);
      const total=first.counts?.matched_cells||0;
      const offsets=[];
      for(let offset=pageSize;offset<total;offset+=pageSize) offsets.push(offset);
      const pages=[];
      for(let index=0;index<offsets.length;index+=4){
        const batch=offsets.slice(index,index+4);
        pages.push(...await Promise.all(batch.map(offset=>{
          const params=new URLSearchParams({limit:String(pageSize),offset:String(offset),template_id:selectedTemplate});
          return fetchJson(API_BASE+`/reporting/catalog/${encodeURIComponent(selectedId)}/cells?${params}`);
        })));
        if(loadId!==cellLoadId.current) return;
      }
      const cells=[...(first.cells||[]),...pages.flatMap(page=>page.cells||[])];
      const layout=await layoutPromise;
      const data={...first,cells,layout,limit:cells.length,offset:0,selected_template_id:selectedTemplate};
      if(loadId!==cellLoadId.current) return;
      setCellData(data);
      setSelectedCell(current=>current&&cells.some(cell=>cell.datapoint_id===current.datapoint_id)?current:null);
    }catch(err){ setError(err.message||String(err)); }
    finally{ if(loadId===cellLoadId.current) setBusy(false); }
  }
  function showCellExplorer(){
    setReportingSurface('cells');
    const selectedTemplate=reportingTemplateForNode(nodeDetail,cellData?.templates)?.template_id||'';
    if(selectedTemplate && selectedTemplate!==cellData?.selected_template_id){
      setCellTemplate(selectedTemplate);
      loadCells({template:selectedTemplate,preferredNode:nodeDetail});
    }else if(!cellData||reportingNodeSelectsTemplate(nodeDetail)&&!selectedTemplate){
      loadCells({preferredNode:nodeDetail});
    }
  }
  async function searchImpactTargets(search=impactQuery){
    setReportingSurface('impact'); setBusy(true); setError('');
    setImpactTarget(null); setImpactData(null);
    try{
      const p=new URLSearchParams({q:search.trim(),limit:'30'});
      for(const type of ['Provision','LegalInstrument','ExternalReference']) p.append('types',type);
      const data=await fetchJson(API_BASE+`/reporting/nodes/search?${p}`);
      setImpactResults(data.results||[]);
    }catch(err){ setError(err.message||String(err)); }
    finally{ setBusy(false); }
  }
  async function loadImpact(target){
    setImpactTarget(target); setImpactData(null); setBusy(true); setError('');
    try{ setImpactData(await fetchJson(API_BASE+`/reporting/impact/${encodeURIComponent(target.node_id||target.id)}?sample_cells=5&limit=80`)); }
    catch(err){ setError(err.message||String(err)); }
    finally{ setBusy(false); }
  }
  function showOverview(){
    const overview=reportingCatalogOverviewGraph(catalog.returns||[]);
    setEdgeGroups(new Set(REPORTING_OVERVIEW_EDGE_GROUP_KEYS));
    setSelectedId(''); setCatalogDetail(null); setReportingSurface('graph'); setCellData(null); setSelectedCell(null); setCatalogOpen(true); setGraph(overview);
    setNodeDetail(overview.nodes.find(node=>node.node_type==='ReportingCollection')||overview.nodes[0]||null);
  }
  function inspectNode(node){ setNodeDetail(node); }
  function focusCollection(name){
    const collection=graph.nodes?.find(node=>node.node_type==='ReportingCollection'&&node.title===name);
    if(collection){ setNodeDetail(collection); setReportingSurface('graph'); }
  }
  function openGraphNode(node){
    if(!selectedId && node?.metadata?.return_id){ const row=(catalog.returns||[]).find(item=>item.return_id===node.metadata.return_id); if(row) openReturn(row); return; }
    setNodeDetail(node);
  }
  function submit(e){ e.preventDefault(); loadCatalog(query); }
  function toggleEdge(group){ const next=new Set(edgeGroups); next.has(group)?next.delete(group):next.add(group); setEdgeGroups(next); }
  function toggleNode(type){ const next=new Set(nodeTypes); const group=reportingNodeTypeGroup(type); const on=group.every(item=>next.has(item)); group.forEach(item=>on?next.delete(item):next.add(item)); setNodeTypes(next); }
  const families=useMemo(()=>{
    const groups=new Map();
    for(const row of catalog.returns||[]){ const group=row.collection_name||row.family;if(!groups.has(group)) groups.set(group,[]); groups.get(group).push(row); }
    return [...groups].map(([name,rows])=>({name,rows}));
  },[catalog.returns]);
  const selectedRow=useMemo(()=>(catalog.returns||[]).find(row=>row.return_id===selectedId)||null,[catalog.returns,selectedId]);
  const requirementEditions=useMemo(
    ()=>reportingRequirementEditions(selectedRow,catalog.returns||[]),
    [selectedRow,catalog.returns],
  );
  return <section className={`reporting-view reporting-surface-${reportingSurface}`}>
    <div className="reporting-toolbar">
      <div className="reporting-product"><span aria-hidden="true">R</span><div><small>Rulebook Explorer</small><h2>PRA Reporting</h2></div></div>
      <div className="reporting-surface-tabs" aria-label="Reporting workspace">
        <button type="button" className={reportingSurface==='graph'?'active':''} onClick={()=>setReportingSurface('graph')}><span aria-hidden="true">⌘</span>Estate</button>
        <button type="button" disabled={!selectedId} className={reportingSurface==='cells'?'active':''} onClick={showCellExplorer}><span aria-hidden="true">▦</span>Cells</button>
        <button type="button" className={reportingSurface==='impact'?'active':''} onClick={()=>{setReportingSurface('impact');if(!impactResults.length&&impactQuery.trim())searchImpactTargets();}}><span aria-hidden="true">↳</span>Impact</button>
      </div>
      <form className="reporting-return-search" onSubmit={submit}><span aria-hidden="true">⌕</span><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Find a return, template or topic"/><button aria-label="Search reporting estate">{busy?'···':'↵'}</button></form>
      <div className="reporting-estate-tabs"><button type="button" className={estate==='supervisory_reporting'?'active':''} onClick={()=>{setEstate('supervisory_reporting');setEdgeGroups(new Set(REPORTING_OVERVIEW_EDGE_GROUP_KEYS));setSelectedId('');setCatalogDetail(null);setNodeDetail(null);setCatalogOpen(true);}}>Returns</button><button type="button" className={estate==='pillar3_disclosure'?'active':''} onClick={()=>{setEstate('pillar3_disclosure');setEdgeGroups(new Set(REPORTING_OVERVIEW_EDGE_GROUP_KEYS));setSelectedId('');setCatalogDetail(null);setNodeDetail(null);setCatalogOpen(true);}}>Pillar 3</button></div>
      <details className="reporting-view-menu"><summary aria-label="Reporting view options" title="View options">•••</summary><div><label><input type="checkbox" checked={includeHistoric} onChange={e=>setIncludeHistoric(e.target.checked)}/> Show superseded editions</label></div></details>
    </div>
    {error&&<div className="error">{error}</div>}
    <div className={`reporting-graph-layout surface-${reportingSurface} ${selectedId?'has-selection':'is-overview'}`}>
      <aside className="reporting-catalog-list reporting-graph-nav">
        <div className="reporting-nav-head">
          <div className="reporting-list-summary"><strong>{catalog.returns?.length||0}</strong><span>{estate==='pillar3_disclosure'?'disclosure sets':'editions'}</span></div>
          {selectedId&&<button className="reporting-overview-button" onClick={showOverview} aria-label="Return to entire reporting estate">←</button>}
        </div>
        {selectedRow&&<div className="reporting-scope-card"><span>Active requirement</span><strong>{selectedRow.return_code}</strong><p>{selectedRow.name}</p>{requirementEditions.length>1&&<label className="reporting-edition-switcher"><span>Edition / history</span><select value={selectedId} onChange={event=>{const row=requirementEditions.find(item=>item.return_id===event.target.value);if(row) openReturn(row);}} aria-label={`Edition of ${selectedRow.return_code}`}>{requirementEditions.map(row=><option key={row.return_id} value={row.return_id}>{reportingEditionOptionLabel(row)}</option>)}</select></label>}<button type="button" onClick={()=>setCatalogOpen(open=>!open)}>{catalogOpen?'Close browser':'Switch return'} <i aria-hidden="true">{catalogOpen?'×':'⌄'}</i></button></div>}
        {selectedId&&reportingSurface==='graph'&&nodeDetail&&<ReportingChildNavigation node={nodeDetail} root={graphRoot} groups={childGroups} parents={parentNodes} onSelect={inspectNode}/>}
        {(!selectedId||catalogOpen)&&<div className="reporting-return-browser">{selectedId&&<h3 className="reporting-browse-heading">Browse returns</h3>}{families.map(group=><section key={group.name}><h3><button type="button" className={!selectedId&&nodeDetail?.node_type==='ReportingCollection'&&nodeDetail.title===group.name?'active':''} onClick={()=>focusCollection(group.name)}>{group.name}</button><span>{group.rows.length}</span></h3>{group.rows.map(row=><button key={row.return_id} className={selectedId===row.return_id?'active':''} onClick={()=>openReturn(row)}><strong>{row.return_code}</strong><span>{row.name}</span>{row.status==='future'&&<em>Future</em>}</button>)}</section>)}</div>}
      </aside>
      <main className="reporting-graph-canvas">
        {reportingSurface==='cells'
          ? <div className="reporting-cell-canvas"><ReportingCellExplorer data={cellData} busy={busy} query={cellQuery} setQuery={setCellQuery} template={cellTemplate} setTemplate={setCellTemplate} onLoad={loadCells} selected={selectedCell} onSelect={setSelectedCell}/></div>
          : reportingSurface==='impact'
          ? <div className="reporting-cell-canvas"><ReportingImpactExplorer query={impactQuery} setQuery={setImpactQuery} results={impactResults} target={impactTarget} data={impactData} busy={busy} onSearch={searchImpactTargets} onSelect={loadImpact}/></div>
          : <><div className="canvas-meta reporting-meta"><strong>{catalogDetail?`${catalogDetail.return_code}: ${catalogDetail.name}`:estate==='pillar3_disclosure'?'Pillar 3 disclosure graph':'Regulatory returns graph'}</strong><span>{activeGraph.nodes.length} nodes · {activeGraph.edges.length} links</span></div><Graph graph={activeGraph} selected={nodeDetail||graphRoot} detail={nodeDetail} nodeTypes={nodeTypes} relationshipTypes={edgeGroups} relationshipFilters={REPORTING_EDGE_GROUPS.map(group=>group.key)} materialFilters={reportingMaterialFilters(graph)} availableEdgeTypes={edgeGroupCounts} onToggleNodeType={toggleNode} onToggleRelationship={toggleEdge} onSelect={openGraphNode} onOpen={openGraphNode} onFeedback={onFeedback}/></>}
      </main>
      <aside className="reporting-graph-inspector">
        {reportingSurface==='cells'
          ? <ReportingCellInfo cell={selectedCell} data={cellData}/>
          : reportingSurface==='impact'
          ? <ReportingImpactInfo target={impactTarget} data={impactData}/>
          : <ReportingGraphInfo node={nodeDetail} catalogDetail={catalogDetail} edges={selectedEdges} graph={activeGraph} onSelect={inspectNode} onFeedback={onFeedback}/>}
      </aside>
    </div>
  </section>;
}

function ReportingImpactExplorer({query,setQuery,results,target,data,busy,onSearch,onSelect}){
  function submit(e){ e.preventDefault(); if(query.trim()) onSearch(query); }
  return <section className="reporting-impact-explorer">
    <header className="reporting-impact-head">
      <span className="eyebrow">Change impact</span>
      <h2>Trace a rule change into reporting</h2>
      <form onSubmit={submit}><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Search for a provision, e.g. Article 4(1), liquidity, own funds…"/><button disabled={!query.trim()||busy}>{busy?'Working…':'Find rule'}</button></form>
    </header>
    {!target&&<div className="reporting-impact-results">
      {results.length>0&&<div className="reporting-impact-result-head"><strong>{results.length} matching graph nodes</strong><span>Choose the exact changed provision before calculating impact.</span></div>}
      {results.map(node=>{const context=node.properties?.description||node.properties?.url;return <button type="button" key={node.node_id} onClick={()=>onSelect(node)}><span>{materialLabel(materialType(node.node_type))}</span><strong>{node.label||node.node_id}</strong>{context&&<small>{truncate(context,180)}</small>}</button>;})}
      {!results.length&&!busy&&<div className="reporting-impact-empty is-prompt">Search the reporting-aware graph for the provision or legal instrument that changed.</div>}
    </div>}
    {target&&busy&&!data&&<div className="reporting-impact-empty">Tracing instruction references and downstream reporting scope…</div>}
    {data&&<ReportingImpactResults data={data}/>}
  </section>;
}

function ReportingImpactResults({data}){
  return <div className="reporting-impact-results-view">
    <div className="reporting-impact-target"><div><span>Changed node</span><strong>{data.target.title}</strong></div><em>{materialLabel(materialType(data.target))}</em></div>
    <div className="reporting-cell-metrics reporting-impact-metrics">
      <div><strong>{fmt(data.counts.affected_returns)}</strong><span>affected returns</span></div>
      <div><strong>{fmt(data.counts.instruction_sources)}</strong><span>instruction sources</span></div>
      <div><strong>{fmt(data.counts.direct_references)}</strong><span>direct references</span></div>
      <div><strong>{fmt(data.counts.direct_coordinates)}</strong><span>direct coordinates</span></div>
      <div><strong>{fmt(data.counts.materialized_direct_cells)}</strong><span>parsed direct cells</span></div>
      <div><strong>{fmt(data.counts.candidate_cells)}</strong><span>candidate cells</span></div>
    </div>
    <div className="reporting-impact-tier-note"><strong>Evidence boundary</strong><span>Direct coordinates mean that the same instruction passage names this rule and row, column or cell. They sharply narrow review but are not automatically confirmed edits; the remaining templates and cells are candidate scope.</span></div>
    <div className="reporting-impact-return-list">{data.returns.map(item=><ReportingImpactReturn key={item.data_item_id} item={item}/>)}</div>
    {!data.returns.length&&<div className="reporting-impact-empty">No reporting instruction in the existing database directly references this node.</div>}
  </div>;
}

function ReportingImpactReturn({item}){
  const [open,setOpen]=useState(false);
  const name=item.catalog_entries?.[0]?.name || item.return_label || item.return_code;
  return <article className="reporting-impact-return">
    <button type="button" className="reporting-impact-return-head" onClick={()=>setOpen(v=>!v)}>
      <span><b>{item.return_code}</b><strong>{name}</strong></span>
      <span className="reporting-impact-badges"><em>{fmt(item.reference_count)} direct ref{item.reference_count===1?'':'s'}</em>{item.direct_coordinate_count>0&&<em className="coordinate">{fmt(item.direct_coordinate_count)} direct coordinate{item.direct_coordinate_count===1?'':'s'}</em>}<em className="candidate">{fmt(item.candidate_cell_count)} candidate cells</em></span>
      <i>{open?'−':'+'}</i>
    </button>
    {open&&<div className="reporting-impact-return-body">
      <section><h3>Instruction sources <span>direct evidence</span></h3><div className="reporting-impact-sources">{item.instruction_sources.map(source=><a key={source.source_id||source.source_node_id} href={source.url||'#'} target="_blank" rel="noopener noreferrer"><strong>{source.title||'Reporting instruction source'}</strong><small>{source.file_type?.toUpperCase()||'SOURCE'}</small><em>Open source ↗</em></a>)}</div></section>
      <section><h3>Reference evidence <span>{item.references_truncated?`showing ${item.references.length} of ${item.reference_count}`:`${item.reference_count} passages`}</span></h3><div className="reporting-impact-evidence">{item.references.slice(0,12).map(ref=><blockquote key={ref.edge_id}><p>{truncate(ref.evidence_text||'Reference detected without extracted passage text.',520)}</p><footer>{[ref.source_title,ref.page_number&&`page ${ref.page_number}`,`${Math.round((ref.confidence||0)*100)}% confidence`].filter(Boolean).join(' · ')}</footer></blockquote>)}</div></section>
      {item.direct_coordinate_count>0&&<section className="reporting-impact-direct-section"><h3>Direct coordinate evidence <span>{item.direct_coordinates_truncated?`showing ${item.direct_coordinates.length} of ${item.direct_coordinate_count}`:`${item.direct_coordinate_count} evidence links`} · review, not confirmed edits</span></h3><div className="reporting-impact-direct-grid">{item.direct_coordinates.map(coordinate=><article key={`${coordinate.legal_edge_id}-${coordinate.coordinate_edge_id}`}><div><strong>{coordinate.template_code}</strong><b>{`r${coordinate.row_code||'—'}${coordinate.column_code?` / c${coordinate.column_code}`:''}`}</b><em className={coordinate.coverage_status==='materialized_datapoint'?'materialized':'defined'}>{coordinate.coverage_status==='materialized_datapoint'?'Parsed cell':'Instruction-defined coordinate'}</em></div><p>{truncate(coordinate.instruction_text||coordinate.evidence_text||'Instruction passage',360)}</p><footer>{truncate([coordinate.row_label,coordinate.column_label,coordinate.source_title,coordinate.page_number&&`page ${coordinate.page_number}`].filter(Boolean).join(' · '),180)}</footer>{(coordinate.source_url||coordinate.template_source_url)&&<a href={coordinate.source_url||coordinate.template_source_url} target="_blank" rel="noopener noreferrer">Open evidence ↗</a>}</article>)}</div></section>}
      <section><h3>Candidate templates and cells <span>review scope, not confirmed edits</span></h3><div className="reporting-impact-templates">{item.templates.map(template=><div key={template.template_id}><strong>{template.template_code}</strong><span>{template.title||template.annex||'Reporting template'}</span><em>{fmt(template.instruction_count)} instructions · {fmt(template.cell_count)} cells</em>{template.source_url&&<a href={template.source_url} target="_blank" rel="noopener noreferrer">Open template ↗</a>}</div>)}</div>{!item.templates.length&&<p className="muted">No parsed cell-bearing template is mapped to this return.</p>}</section>
    </div>}
  </article>;
}

function ReportingImpactInfo({target,data}){
  if(!target&&!data) return <div className="pane reporting-cell-info reporting-impact-primer"><span className="eyebrow">Impact method</span><h2>Evidence before inference</h2><ol><li><i>01</i><div><strong>Direct reference</strong><span>An instruction expressly names the changed rule.</span></div></li><li><i>02</i><div><strong>Direct coordinate</strong><span>The same passage identifies a row, column or cell.</span></div></li><li><i>03</i><div><strong>Candidate scope</strong><span>Mapped templates and cells are kept separate for review.</span></div></li></ol></div>;
  const node=data?.target||target;
  return <div className="pane reporting-impact-info">
    <span className="kind">Changed graph node</span>
    <h2>{node?.title||node?.label||node?.node_id}</h2>
    {data&&<><div className="reporting-impact-model"><h3>Direct instruction reference</h3><p>{data.impact_model.direct_instruction_reference}</p><h3>Direct coordinate evidence</h3><p>{data.impact_model.direct_coordinate_evidence}</p><h3>Candidate scope</h3><p>{data.impact_model.candidate_scope}</p></div><Collapsible title="Limitations" count={`${data.limitations.length}`} open><ul className="reporting-impact-limitations">{data.limitations.map(text=><li key={text}>{text}</li>)}</ul></Collapsible></>}
  </div>;
}

function ReportingCellExplorer({data,busy,query,setQuery,template,setTemplate,onLoad,selected,onSelect}){
  if(!data) return <div className="reporting-cell-loading">{busy?'Loading cell-level reporting data…':'Open a reporting edition to explore its cells.'}</div>;
  const coverage=reportingCellCoverage(data.coverage);
  const selectedTemplate=(data.templates||[]).find(item=>item.template_id===(data.selected_template_id||template))||(data.coverage==='selected_template_unavailable'?null:data.templates?.[0])||null;
  const grid=reportingTemplateGrid(data.cells||[],query);
  function submit(e){ e.preventDefault(); }
  function chooseTemplate(e){ const next=e.target.value;setTemplate(next);setQuery('');onLoad({template:next}); }
  return <section className="reporting-cell-explorer">
    <header className="reporting-cell-head">
      <div><span className="eyebrow">Cell explorer</span><h2>{data.return.return_code}: {data.return.name}</h2></div>
      <div className={`reporting-cell-coverage ${coverage.tone}`}><strong>{coverage.title}</strong><span>{coverage.detail}</span></div>
    </header>
    <div className="reporting-cell-metrics">
      <div><strong>{fmt(data.counts.templates)}</strong><span>parsed templates</span></div>
      <div><strong>{fmt(data.counts.cells)}</strong><span>cells in this edition</span></div>
      <div><strong>{fmt(query?grid.matchingCells:grid.populatedCells)}</strong><span>{query?'matching this search':'positioned cells in this template'}</span></div>
    </div>
    <form className="reporting-cell-search" onSubmit={submit}>
      <label><span>Template</span><select value={selectedTemplate?.template_id||template} onChange={chooseTemplate}>{data.coverage==='selected_template_unavailable'&&<option value="">{data.requested_template?.title||'Selected template'} — no parsed cells</option>}{data.templates.map(item=><option key={item.template_id} value={item.template_id}>{item.template_code}{reportingTemplateTitle(item)!==item.template_code?` — ${reportingTemplateTitle(item)}`:''} ({fmt(item.cell_count)})</option>)}</select></label>
      <label><span>Find within this template</span><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Search row, column, concept or coordinate…"/></label>
      <button type="submit" disabled={busy}>{busy?'Loading template…':'Find in template'}</button>
    </form>
    {['available','template_layout_available'].includes(data.coverage)?<>
      <div className="reporting-cell-result-head"><strong>{query?`${fmt(grid.matchingCells)} matching cells`:(data.layout?.format==='pdf'?`${fmt(data.layout.page_count)} page official PDF template`:data.layout?`${fmt(data.layout.rows.length)} worksheet rows × ${fmt(data.layout.columns.length)} worksheet columns`:`${fmt(grid.allRows.length)} rows × ${fmt(grid.columns.length)} columns`)}</strong><span>{data.layout?.format==='pdf'?'Rendered directly from the official PDF template.':data.layout?'Rendered from the official workbook, including its merged headers, dimensions and cell styles.':'Rows and columns retain the template’s reporting coordinates.'} {data.cells?.length?'Choose a populated cell for its complete path.':''}</span></div>
      <ReportingTemplateMatrix template={selectedTemplate} layout={data.layout} cells={data.cells||[]} grid={grid} query={query} selected={selected} onSelect={onSelect}/>
    </>:<ReportingCellCoverageDetails data={data}/>}
  </section>;
}

function ReportingTemplateMatrix({template,layout,cells,grid,query,selected,onSelect}){
  const gridRef=useRef(null);
  useEffect(()=>{
    if(gridRef.current) gridRef.current.scrollTo({top:0,left:0});
  },[template?.template_id,query]);
  return <section className="reporting-template-matrix">
    <header>
      <div><span>{template?.template_code||'Template'}</span><h3>{reportingTemplateTitle(template)}</h3></div>
      <p>{layout?.format==='pdf'?`Official PDF · ${fmt(layout.page_count)} page${layout.page_count===1?'':'s'}`:layout?`${layout.sheet_name} worksheet · ${fmt(layout.rows.length)} rows · ${fmt(layout.columns.length)} columns`:`${fmt(grid.populatedCoordinates)} populated positions · ${fmt(grid.allRows.length)} rows · ${fmt(grid.columns.length)} columns${grid.unpositionedCells?` · ${fmt(grid.unpositionedCells)} cells without complete coordinates excluded`:''}`}</p>
      {template?.source_url&&<a href={template.source_url} target="_blank" rel="noopener noreferrer">Open official template ↗</a>}
    </header>
    {template&&layout?(layout.format==='pdf'?<ReportingPdfTemplate template={template} layout={layout}/>:<ReportingWorkbookTemplate layout={layout} cells={cells} query={query} selected={selected} onSelect={onSelect}/>):<div className="reporting-template-grid-wrap" ref={gridRef}>
      <table className="reporting-template-grid" aria-label={`${template?.template_code||'Reporting'} template cells`}>
        <thead><tr><th className="reporting-template-corner"><span>Row</span><strong>Reported item</strong></th>{grid.columns.map(column=><th key={column.id} scope="col"><code>c{column.code}</code><span>{column.label}</span></th>)}</tr></thead>
        <tbody>
          {grid.rows.map(row=><tr key={row.id}>
            <th scope="row"><code>r{row.code}</code><span>{row.label}</span></th>
            {grid.columns.map(column=>{
              const cells=grid.cellsByCoordinate.get(`${row.id}\u0000${column.id}`)||[];
              const cell=cells[0];
              if(!cell) return <td key={column.id} className="empty" aria-label={`No cell at r${row.code} / c${column.code}`}/>;
              const active=selected?.datapoint_id===cell.datapoint_id;
              const match=query&&cells.some(item=>grid.matchingIds.has(item.datapoint_id));
              return <td key={column.id} className={`${active?'active ':''}${match?'match':''}`.trim()}><button type="button" onClick={()=>onSelect(cell)} title={`${reportingCellCoordinate(cell)} — ${reportingCellTitle(cell)}`}><span>{reportingCellTitle(cell)}</span>{cells.length>1&&<b>+{cells.length-1}</b>}</button></td>;
            })}
          </tr>)}
          {!grid.rows.length&&<tr><td className="reporting-template-no-match" colSpan={Math.max(1,grid.columns.length+1)}>No cells match this search. Try another row code, column code, concept or coordinate.</td></tr>}
        </tbody>
      </table>
    </div>}
  </section>;
}

function ReportingPdfTemplate({template,layout}){
  const documentUrl=`${API_BASE}/reporting/templates/${encodeURIComponent(template.template_id)}/document#page=1&toolbar=0&navpanes=0&view=FitH`;
  return <div className="reporting-pdf-template">
    <iframe src={documentUrl} title={`${template.template_code||'Reporting'} official PDF template`}/>
    <p>{fmt(layout.page_count)} page{layout.page_count===1?'':'s'} · rendered from the official PDF file</p>
  </div>;
}

function ReportingWorkbookTemplate({layout,cells,query,selected,onSelect}){
  const [scrollTop,setScrollTop]=useState(0);
  const workbook=useMemo(()=>reportingWorkbookDatapoints(layout,cells),[layout,cells]);
  const scale=(Number(layout.zoom)||100)/100;
  const columnWidths=useMemo(()=>new Map(
    (layout.columns||[]).map(column=>[
      column.index,
      reportingWorkbookColumnPixels(column.width,layout.zoom),
    ]),
  ),[layout]);
  const rowHeights=useMemo(()=>new Map(
    (layout.rows||[]).map(row=>[
      row.index,
      Math.max(1,Math.round((Number(row.height)||layout.default_row_height||15)*96/72*scale)),
    ]),
  ),[layout,scale]);
  const frozenRows=new Set((layout.rows||[]).slice(0,layout.freeze?.rows||0).map(row=>row.index));
  const frozenColumns=new Set((layout.columns||[]).slice(0,layout.freeze?.columns||0).map(column=>column.index));
  const rowTops=new Map();
  let rowTop=0;
  for(const row of layout.rows||[]){rowTops.set(row.index,rowTop);rowTop+=rowHeights.get(row.index)||0;}
  const columnLefts=new Map();
  let columnLeft=0;
  for(const column of layout.columns||[]){columnLefts.set(column.index,columnLeft);columnLeft+=columnWidths.get(column.index)||0;}
  const allRows=layout.rows||[];
  let visibleRows=allRows;
  let topSpacer=0;
  let bottomSpacer=0;
  if(layout.sparse){
    const viewportTop=Math.max(0,scrollTop-500);
    const viewportBottom=scrollTop+1400;
    let first=0;
    while(first<allRows.length&&(rowTops.get(allRows[first].index)||0)+(rowHeights.get(allRows[first].index)||0)<viewportTop) first+=1;
    let last=first;
    while(last<allRows.length&&(rowTops.get(allRows[last].index)||0)<viewportBottom) last+=1;
    visibleRows=allRows.slice(first,last);
    topSpacer=visibleRows.length?(rowTops.get(visibleRows[0].index)||0):rowTop;
    const visibleEnd=visibleRows.length?(rowTops.get(visibleRows.at(-1).index)||0)+(rowHeights.get(visibleRows.at(-1).index)||0):rowTop;
    bottomSpacer=Math.max(0,rowTop-visibleEnd);
  }
  function workbookCells(row){
    if(!layout.sparse) return row.cells;
    const explicit=new Map((row.cells||[]).map(cell=>[cell.column,cell]));
    return (layout.columns||[]).flatMap(column=>{
      const merge=(layout.merged_ranges||[]).find(range=>(
        range.start_row<=row.index&&row.index<=range.end_row
        &&range.start_column<=column.index&&column.index<=range.end_column
      ));
      if(merge&&(merge.start_row!==row.index||merge.start_column!==column.index)) return [];
      const cell=explicit.get(column.index)||{
        reference:`${column.letter}${row.index}`,
        column:column.index,
        value:'',
        raw_value:'',
        formula:null,
        style_id:row.style_id||column.style_id||0,
      };
      if(merge) return [{...cell,row_span:merge.end_row-merge.start_row+1,column_span:merge.end_column-merge.start_column+1}];
      return [cell];
    });
  }
  const needle=String(query||'').trim().toLowerCase();
  return <div className="reporting-workbook-wrap" aria-label={`${layout.template_code||layout.sheet_name} source workbook sheet`} onScroll={layout.sparse?event=>setScrollTop(event.currentTarget.scrollTop):undefined}>
    <table className="reporting-workbook-sheet" style={{width:`${columnLeft}px`}}>
      <colgroup>{(layout.columns||[]).map(column=><col key={column.index} style={{width:`${columnWidths.get(column.index)}px`,display:column.hidden?'none':undefined}}/>)}</colgroup>
      <tbody>
        {topSpacer>0&&<tr className="reporting-workbook-spacer" style={{height:`${topSpacer}px`}}><td colSpan={layout.columns.length}/></tr>}
        {visibleRows.map(row=><tr key={row.index} style={{height:`${rowHeights.get(row.index)}px`,display:row.hidden?'none':undefined}}>
        {workbookCells(row).map(cell=>{
          const datapoint=workbook.cellFor(row,cell);
          const style=reportingWorkbookCellStyle(layout.styles?.[cell.style_id]||{},layout.zoom);
          const active=datapoint&&selected?.datapoint_id===datapoint.datapoint_id;
          const haystack=[cell.value,datapoint?.concept_label,datapoint?.row_label,datapoint?.column_label].filter(Boolean).join(' ').toLowerCase();
          const match=needle&&haystack.includes(needle);
          const stickyRow=frozenRows.has(row.index);
          const stickyColumn=frozenColumns.has(cell.column);
          const stickyStyle=stickyRow||stickyColumn?{
            position:'sticky',
            top:stickyRow?`${rowTops.get(row.index)}px`:undefined,
            left:stickyColumn?`${columnLefts.get(cell.column)}px`:undefined,
            zIndex:stickyRow&&stickyColumn?4:stickyRow?3:2,
          }:{};
          const content=<span className="reporting-workbook-cell-content" style={{
            justifyContent:style.justifyContent,
            alignItems:style.alignItems,
            whiteSpace:style.whiteSpace,
            paddingLeft:style.paddingLeft,
            transform:style.transform,
            writingMode:style.writingMode,
          }}>{cell.value}</span>;
          return <td key={cell.reference}
            rowSpan={cell.row_span||1}
            colSpan={cell.column_span||1}
            className={`${datapoint?'has-datapoint ':''}${active?'active ':''}${match?'match':''}`.trim()}
            style={{...style,...stickyStyle}}
            title={datapoint?`${cell.reference} · ${reportingCellCoordinate(datapoint)}`:cell.reference}>
            {datapoint?<button type="button" onClick={()=>onSelect(datapoint)}>{content}</button>:content}
          </td>;
        })}
      </tr>)}
        {bottomSpacer>0&&<tr className="reporting-workbook-spacer" style={{height:`${bottomSpacer}px`}}><td colSpan={layout.columns.length}/></tr>}
      </tbody>
    </table>
  </div>;
}

function ReportingCellCoverageDetails({data}){
  return <div className="reporting-cell-unavailable">
    <h3>What is available</h3>
    {data.templates?.length?<div className="reporting-template-coverage">{data.templates.map(item=><article key={item.template_id}><div><strong>{item.template_code}</strong><span>{item.title}</span></div><em>{fmt(item.row_count)} rows · {fmt(item.column_count)} columns · {fmt(item.cell_count)} cells</em>{item.source_url&&<a href={item.source_url} target="_blank" rel="noopener noreferrer">Open official template ↗</a>}</article>)}</div>:<p>The catalogue entry and official resources are available, but no parsed template is linked to this edition yet.</p>}
  </div>;
}

function ReportingCellInfo({cell,data}){
  if(!cell) return <div className="pane reporting-cell-info"><span className="eyebrow">Cell information</span><h2>Select a cell</h2><p className="muted">Choose any result to see how it sits inside the return, its row and column meaning, datatype, unit and official source.</p></div>;
  const path=reportingCellPath(cell,data?.return);
  const template=(data?.templates||[]).find(item=>item.template_id===cell.template_id);
  return <div className="pane reporting-cell-info">
    <span className="kind">Reporting cell</span>
    <h2>{reportingCellTitle(cell)}</h2>
    <code className="reporting-cell-coordinate">{reportingCellCoordinate(cell)}</code>
    <div className="reporting-cell-path">{path.map((item,index)=><div key={`${item.kind}:${item.code}`}><i>{index+1}</i><span>{item.kind}</span><strong>{item.code}</strong><p>{item.label}</p></div>)}</div>
    <div className="reporting-cell-properties">
      <div><span>Datatype</span><strong>{cell.data_type||'Not recorded'}</strong></div>
      <div><span>Unit</span><strong>{cell.unit_type||'Not recorded'}</strong></div>
      <div><span>Datapoint ID</span><strong>{cell.datapoint_id}</strong></div>
    </div>
    {template?.source_url&&<a className="reporting-cell-source" href={template.source_url} target="_blank" rel="noopener noreferrer"><span>Official template source</span><strong>{template.source_title||template.title||template.template_code}</strong><em>Open file ↗</em></a>}
  </div>;
}

function ReportingChildNavigation({node,root,groups,parents,onSelect}){
  const childCount=groups.reduce((count,group)=>count+group.children.length,0);
  const backTargets=parents.filter(parent=>parent.id!==root?.id);
  return <nav className="reporting-child-nav" aria-label="Selected node children">
    <div className="reporting-child-current"><span>Selected node</span><strong>{displayNodeTitle(node)}</strong><small>{materialLabel(materialType(node))}</small></div>
    <div className="reporting-child-actions">
      {root&&node.id!==root.id&&<button type="button" onClick={()=>onSelect(root)}>↑ Return root</button>}
      {backTargets.slice(0,2).map(parent=><button type="button" key={parent.id} onClick={()=>onSelect(parent)}>↑ {displayNodeTitle(parent)}</button>)}
    </div>
    <h3>Child nodes <span>{childCount}</span></h3>
    {groups.map(group=><section key={group.edgeType}>
      <h4>{group.label||relationLabel(group.edgeType)}<span>{group.children.length}</span></h4>
      <div className="reporting-child-list">{group.children.map(child=><button type="button" key={child.id} onClick={()=>onSelect(child)}><strong>{displayNodeTitle(child)}</strong><small>{materialLabel(materialType(child))}</small></button>)}</div>
    </section>)}
    {childCount===0&&<p className="reporting-child-empty">This is a leaf node. Use Return root or the graph to continue navigating.</p>}
  </nav>;
}

function reportingCatalogOverviewGraph(returns){
  const groups=new Map();
  for(const row of returns){
    const key=row.collection_id||row.collection_name||row.family||'reporting';
    if(!groups.has(key)) groups.set(key,{id:`collection:${key}`,node_type:'ReportingCollection',title:row.collection_name||row.family||'Reporting',text:'',metadata:{collection_id:row.collection_id||key,return_count:0}});
    groups.get(key).metadata.return_count+=1;
  }
  const groupNodes=[...groups.values()].map(node=>({...node,text:`${node.metadata.return_count} reporting edition${node.metadata.return_count===1?'':'s'}`}));
  const returnNodes=returns.map(row=>({id:row.edition_id||row.return_id,node_type:'RequirementEdition',title:row.edition_display_name||`${row.return_code} — ${row.name}`,text:row.description||'',url:row.source_page_url||'',metadata:{...row,return_id:row.return_id,data_item_code:row.return_code}}));
  const edges=returns.map(row=>{const key=row.collection_id||row.collection_name||row.family||'reporting';return {id:`overview:${key}:${row.edition_id||row.return_id}`,from_node_id:`collection:${key}`,to_node_id:row.edition_id||row.return_id,edge_type:'HAS_EDITION',confidence:1,source_method:'reporting_catalog'};});
  return {level:'reporting_ontology',nodes:[...groupNodes,...returnNodes],edges,available_edge_types:{HAS_EDITION:edges.length}};
}

function ReportingGraphInfo({node,catalogDetail,edges,graph,onSelect,onFeedback}){
  if(!node) return <div className="pane reporting-graph-info"><span className="eyebrow">Node information</span><h2>Select a node</h2><p className="muted">Click a return, template, instruction, taxonomy or Rulebook node to inspect it here.</p></div>;
  const isRoot=['RequirementEdition','DataItem','ReportingReturn','DisclosureSet'].includes(node.node_type);
  const neighbours=new Map((graph.nodes||[]).map(item=>[item.id,item]));
  const links=reportingSourceUrls(node,edges,graph);
  const templates=(catalogDetail?.artifacts||[]).filter(a=>a.relationship==='template');
  const instructions=(catalogDetail?.artifacts||[]).filter(a=>a.relationship==='instructions');
  const contextualName=isRoot?'':(edges||[]).map(edge=>edge.metadata?.display_name).find(Boolean);
  const visibleTitle=contextualName||displayNodeTitle(node);
  return <div className="pane reporting-graph-info">
    <span className="kind">{materialLabel(materialType(node))}</span>
    <h2>{visibleTitle}</h2>
    {contextualName&&contextualName!==displayNodeTitle(node)&&<p className="muted">Official resource: {displayNodeTitle(node)}</p>}
    {(isRoot?catalogDetail?.description:node.text)&&<p className="text">{truncate(isRoot?catalogDetail.description:node.text,1500)}</p>}
    {isRoot&&catalogDetail?.effective_text&&<span className="reporting-effective">Effective {catalogDetail.effective_text}</span>}
    {isRoot&&<><ReportingInfoLinks title="Templates" items={templates}/><ReportingInfoLinks title="Instructions" items={instructions}/></>}
    {!isRoot&&links.length>0&&<Collapsible title="Source files" count={`${links.length}`} open><div className="source-link-list">{links.slice(0,12).map(link=><a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer"><span>{link.kind}</span><strong>{link.label}</strong><em>Open source ↗</em></a>)}</div></Collapsible>}
    {edges.length>0&&<Collapsible title="Connected nodes" count={`${edges.length}`} open><div className="edge-list">{edges.slice(0,30).map(edge=>{const other=neighbours.get(edge.from_node_id===node.id?edge.to_node_id:edge.from_node_id);return <button key={edge.id} type="button" onClick={()=>other&&onSelect(other)}><span>{relationLabel(edge.edge_type)}</span><strong>{displayNodeTitle(other||{})}</strong></button>})}</div></Collapsible>}
    <button className="reporting-info-feedback" onClick={()=>onFeedback(node)}>⚑ Report an issue with this node</button>
  </div>;
}

function ReportingInfoLinks({title,items}){
  if(!items.length) return null;
  return <Collapsible title={title} count={`${items.length}`} open><div className="source-link-list">{items.map(item=><a key={item.artifact_id} href={item.url} target="_blank" rel="noopener noreferrer"><span>{String(item.file_type||'file').toUpperCase()}</span><strong>{item.resolved_display_name||item.display_title}</strong>{item.resolved_display_name&&item.resolved_display_name!==item.display_title&&<small>Official resource: {item.display_title}</small>}{item.sheet_names?.length>0&&<small>{item.sheet_names.join(' · ')}</small>}<em>Open official file ↗</em></a>)}</div></Collapsible>;
}

function reportingSourceHaystack(node){
  const md=node?.metadata||{};
  return [node?.title,node?.text,node?.url,md.source_url,md.url,md.document_url,md.original_url,md.target_url,md.source_local_path,md.local_path,md.source_title,md.title,md.file_type,md.source_file_type].filter(Boolean).join(' ').toLowerCase();
}

function isWorkbookSourceDocument(node){
  if(!['SourceDocument','TemplateSet'].includes(node?.node_type)) return false;
  return /\.(xlsx|xlsm|xltx)(#|\?|$)/i.test(reportingSourceHaystack(node)) || ['xlsx','xlsm','xltx'].includes(String(node?.metadata?.file_type||node?.metadata?.source_file_type||'').toLowerCase());
}

function isPdfSourceDocument(node){
  if(node?.node_type!=='SourceDocument') return false;
  return /\.pdf(#|\?|$)/i.test(reportingSourceHaystack(node)) || String(node?.metadata?.file_type||node?.metadata?.source_file_type||'').toLowerCase()==='pdf';
}

function isTaxonomySourceDocument(node){
  if(!['SourceDocument','TemplateSet'].includes(node?.node_type)) return false;
  return /\.(xml|xsd|zip|xbrl)(#|\?|$)/i.test(reportingSourceHaystack(node)) || /\b(taxonomy|xbrl|dpm)\b/i.test(reportingSourceHaystack(node));
}

function reportingNodeTypeGroup(t){
  return ({
    reporting_estate:['ReportingEstate'],
    reporting_regime:['ReportingRegime'],
    reporting_collection:['ReportingCollection'],
    reporting_requirement:['ReportingRequirement'],
    reporting_edition:['RequirementEdition','DataItem','ReportingReturn','DisclosureSet'],
    reporting_resource:['ReportingResource','SourceDocument'],
    reporting_return:['ReportingRequirement','RequirementEdition','DataItem','ReportingReturn','DisclosureSet'],
    reporting_template:['LogicalTemplate','Template','TemplateSet'],
    reporting_instruction:['InstructionSet'],
    reporting_source:['SourceDocument'],
    reporting_xbrl_source:['TaxonomyRelease','ReportingResource','SourceDocument','TemplateSet'],
    reporting_datapoint:['DataPointGroup','DataPoint','TemplateRow','TemplateColumn'],
    reporting_provision:['Provision'],
    reporting_concept:['Concept','ScopeRule','FirmType','Metric','CalculationRule','ValidationRule'],
    legal_instrument:['LegalInstrument'],
    permission:['Permission'],
    external_reference:['ExternalReference'],
  }[t]||[t]);
}

function reportingMaterialFilters(graph){
  const order=['reporting_estate','reporting_regime','reporting_collection','reporting_requirement','reporting_edition','reporting_resource','reporting_template','reporting_instruction','reporting_xbrl_source','reporting_source','reporting_datapoint','reporting_provision','reporting_concept','legal_instrument','permission','external_reference'];
  const present=new Set((graph.nodes||[]).map(n=>materialType(n)));
  return order.filter(t=>present.has(t));
}

function reportingSourceUrls(node,edges,graph){
  const byId=new Map((graph?.nodes||[]).map(n=>[n.id,n]));
  const links=[];
  const add=(url,label,kind='Source document')=>{
    const clean=String(url||'').trim();
    if(!/^https?:\/\//i.test(clean) || links.some(l=>l.url===clean)) return;
    links.push({url:clean,label:label||'Source document',kind});
  };
  const addNode=(candidate)=>{
    if(!candidate) return;
    const md=candidate.metadata||{};
    if(['ReportingResource','SourceDocument','InstructionSet','Template','TemplateSet'].includes(candidate.node_type)){
      for(const key of ['source_url','url','document_url','original_url','target_url']) add(md[key]||candidate[key],reportingSourceLinkLabel(candidate),reportingUrlKind(candidate));
    }
  };
  addNode(node);
  for(const source of reportingSourceNodes(node,graph)) addNode(source);
  for(const edge of edges||[]){
    const other=byId.get(edge.from_node_id===node?.id?edge.to_node_id:edge.from_node_id);
    addNode(other);
    add(edge.source_url,relationLabel(edge.edge_type),relationLabel(edge.edge_type));
    for(const key of ['source_url','url','document_url','original_url','target_url']) add(edge.metadata?.[key],relationLabel(edge.edge_type),relationLabel(edge.edge_type));
  }
  return links.sort((a,b)=>a.kind.localeCompare(b.kind)||a.label.localeCompare(b.label));
}

function reportingUrlKind(node){
  if(isWorkbookSourceDocument(node)||node?.node_type==='Template') return 'Template workbook';
  if(isPdfSourceDocument(node)||node?.node_type==='InstructionSet') return 'Instructions and guidance';
  if(isTaxonomySourceDocument(node)||node?.node_type==='TemplateSet') return 'Taxonomy';
  return materialLabel(materialType(node));
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
  ctx.fillStyle=visual==='template'?COLOURS.successSoft:visual==='instruction'?COLOURS.warningSoft:visual==='xbrl_source'?COLOURS.infoSoft:badge?.kind==='pdf'?COLOURS.error:badge?.kind==='spreadsheet'?COLOURS.success:role==='parent'?COLOURS.errorSoft:node.colour||nodeFill(raw,{});
  ctx.fill();
  ctx.lineWidth=selectedNode?4:visual?3:role==='parent'?3:2;
  ctx.setLineDash(!visual&&(role==='parent'||role==='child')?[4,3]:[]);
  ctx.strokeStyle=selectedNode?COLOURS.accent:visual==='template'?COLOURS.success:visual==='instruction'?COLOURS.warning:visual==='xbrl_source'?COLOURS.purple:role==='parent'?COLOURS.error:role==='child'?COLOURS.accent:'rgba(255,255,255,.92)';
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
  ctx.strokeStyle=visual==='template'?COLOURS.success:visual==='instruction'?COLOURS.warning:COLOURS.purple;
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
  ctx.fillStyle=`${COLOURS.brandRaised}`;
  roundedRectPath(ctx,x-width/2,y-height/2,width,height,height/2); ctx.fill();
  ctx.strokeStyle=COLOURS.accent; ctx.lineWidth=1.5/globalScale; ctx.stroke();
  ctx.fillStyle=COLOURS.white; ctx.fillText(label,x,y+.5/globalScale);
  ctx.restore();
}
function drawCanvasLabel(ctx,text,x,y,fontSize,globalScale,strong=false,minFontSize=8){
  const size=Math.max(minFontSize,fontSize/globalScale);
  ctx.font=`${strong?'800':'700'} ${size}px Inter, system-ui, sans-serif`;
  ctx.textAlign='center'; ctx.textBaseline='middle';
  const width=Math.min(220/globalScale,ctx.measureText(text).width+10/globalScale);
  const height=size+7/globalScale;
  ctx.fillStyle='rgba(255,255,255,.95)';
  roundedRectPath(ctx,x-width/2,y-height/2,width,height,5/globalScale); ctx.fill();
  ctx.strokeStyle='rgba(63,94,78,.55)'; ctx.lineWidth=1/globalScale; ctx.stroke();
  ctx.fillStyle=COLOURS.brand; ctx.fillText(text,x,y);
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

function Explore({node,edges,graph,onChoose,onRead,onReportIssue}){
  const [inspectorTab,setInspectorTab]=useState('selected-node');
  return <div className="pane explore-pane">
    <div className="inspector-tabs" role="tablist" aria-label="Inspector views">
      <button type="button" id="inspector-connections-tab" role="tab" aria-selected={inspectorTab==='connections'} aria-controls="inspector-connections-panel" tabIndex={inspectorTab==='connections'?0:-1} className={inspectorTab==='connections'?'active':''} onClick={()=>setInspectorTab('connections')}>Connections</button>
      <button type="button" id="inspector-selected-node-tab" role="tab" aria-selected={inspectorTab==='selected-node'} aria-controls="inspector-selected-node-panel" tabIndex={inspectorTab==='selected-node'?0:-1} className={inspectorTab==='selected-node'?'active':''} onClick={()=>setInspectorTab('selected-node')}>Selected node</button>
    </div>
    {inspectorTab==='connections'
      ? <div id="inspector-connections-panel" role="tabpanel" aria-labelledby="inspector-connections-tab"><ConnectionsOverview node={node} edges={edges} graph={graph} onChoose={onChoose}/></div>
      : <div id="inspector-selected-node-panel" role="tabpanel" aria-labelledby="inspector-selected-node-tab"><SelectedNodeDetails node={node} edges={edges} onRead={onRead} onReportIssue={onReportIssue}/></div>}
  </div>;
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

function ConnectionsOverview({node,edges,graph,onChoose}){
  const byId=new Map((graph?.nodes||[]).map(n=>[n.id,n]));
  if(!node)return <section className="explore-layer evidence-layer"><p className="muted">Select a node.</p></section>;
  const analytical=edges.filter(e=>e.edge_type!=='contains');
  const groups=groupEdges(analytical);
  return <section className="explore-layer evidence-layer connections-overview" aria-label="Connections">
    <span className="kind">{label(node.node_type)}</span>
    <h2><NodeTitle node={node}/></h2>
    {groups.length
      ? groups.map(([edgeType,items],i)=><Collapsible key={edgeType} title={edgeType==='references'?'Cross-references':evidenceLabel(edgeType)} count={`${items.length} link${items.length===1?'':'s'}`} open={i<2}>
          <div className="edge-list">{items.slice(0,40).map(e=>{const other=byId.get(e.from_node_id===node.id?e.to_node_id:e.from_node_id);return <button key={e.id} className={`edge-direction-${edgeDirectionLabel(e,node.id)}`} onClick={()=>other&&onChoose(other)}><span><b className="edge-arrow">{edgeDirectionGlyph(e,node.id)}</b>{edgeSummary(e,node.id)}</span><strong><NodeTitle node={other}/></strong>{edgeContext(e,other)&&<small>{edgeContext(e,other)}</small>}{e.evidence_text&&<small>{truncate(e.evidence_text,160)}</small>}</button>})}</div>
          {items.length>40&&<p className="muted">Showing first 40 of {items.length} visible links. Increase the graph cap to load more.</p>}
        </Collapsible>)
      : <Collapsible title="Visible connections" count="0 links" open><p className="muted">No reference, definition or obligation links are visible for this node under the current settings.</p></Collapsible>}
  </section>;
}

function SelectedNodeDetails({node,edges,onRead,onReportIssue}){
  if(!node)return <section className="explore-layer evidence-layer"><p className="muted">Select a node.</p></section>;
  return <section className="explore-layer evidence-layer selected-node-details" aria-label="Selected node details">
    <div className="selected-node-heading"><span>Selected node</span></div>
    <Collapsible title="Selected material" count={label(node.node_type)} open>
      <span className="kind">{label(node.node_type)}</span><h2><NodeTitle node={node}/></h2>{node.url&&<a className="source" href={node.url} target="_blank" rel="noopener noreferrer">Open source ↗</a>}
      <p className="text">{node.text?truncate(node.text,1300):emptyNodeMessage(node,edges)}</p>
      <button type="button" className="reading-mode-entry" onClick={()=>onRead(node)}><span>Reading mode</span><strong>Read this provision with its references →</strong></button>
      <button type="button" className="report-issue-btn" onClick={()=>onReportIssue?.(node)}>⚑ Report an issue with this node</button>
    </Collapsible>
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
  if(graph?.level==='part' || graph?.level==='article') return CHART_SEQUENCE[(n.metadata?.semantic_cluster??0)%CHART_SEQUENCE.length];
  return MATERIAL_COLOURS[materialType(n)]||COLOURS.brandMid;
}
function emptyNodeMessage(node,edges=[]){
  if(['part','chapter','guidance_document','guidance_section','rulebook'].includes(node?.node_type)){
    const hasOutgoingChild=edges.some(edge=>edge.edge_type==='contains'&&edge.from_node_id===node?.id);
    if(hasOutgoingChild) return 'This is a heading or container node. The substantive legal text is held in the child provision nodes shown in the left-hand contents panel.';
    return 'This is a heading or container node. No child provision text is currently linked for this heading.';
  }
  if(node?.metadata?.placeholder) return 'This is a placeholder reference node. Open the source link for the external definition or referenced material.';
  return 'No body text for this node.';
}
function edgeColour(v){return EDGE_COLOURS[v]||COLOURS.brandSoft}
function edgeDirectionColour(e,currentId){
  const dir=edgeDirectionLabel(e,currentId);
  if(dir==='incoming') return COLOURS.error;
  if(dir==='outgoing') return COLOURS.accent;
  return e.visual?.colour||edgeColour(e.edge_type);
}
function relationLabel(v){
  const reportingGroup=reportingEdgeGroup(v)||REPORTING_EDGE_GROUPS.find(group=>group.key===v);
  return reportingGroup?.label||RELATION_LABELS[v]||String(v||'').replaceAll('_',' ');
}
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
    legal_reference_occurrence_v1:'exact legal citation',
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
    rule:['rule','provision','chapter','part','rulebook'],
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
  if(type==='ReportingEstate') return 'reporting_estate';
  if(type==='ReportingRegime') return 'reporting_regime';
  if(type==='ReportingCollection') return 'reporting_collection';
  if(type==='ReportingRequirement') return 'reporting_requirement';
  if(type==='RequirementEdition' || type==='DataItem' || type==='ReportingReturn' || type==='DisclosureSet') return 'reporting_edition';
  if(type==='ReportingResource') return meta.resource_role?.includes('instructions')?'reporting_instruction':meta.resource_role?.includes('taxonomy')?'reporting_xbrl_source':'reporting_resource';
  if(type==='Worksheet' || type==='LogicalTemplate') return 'reporting_template';
  if(type==='InstructionSection') return 'reporting_instruction';
  if(type==='TaxonomyRelease' || type==='TaxonomyEntryPoint') return 'reporting_xbrl_source';
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
  if(['rule','provision','chapter','part','rulebook'].includes(type)) return 'rule';
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
function materialLabel(v){return ({rule:'Rulebook part / rule',supervisory_statement:'Supervisory statement',statement_of_policy:'Statement of policy',definition:'Definition',permission:'Firm permission',external_reference:'External reference',legal_instrument:'Legal instrument',obligation_pattern:'Obligation pattern',obligation_statement:'Structured obligation',analysis:'Obligation marker',reporting_estate:'Reporting estate',reporting_regime:'Reporting regime',reporting_collection:'Reporting collection',reporting_requirement:'Reporting requirement',reporting_edition:'Requirement edition',reporting_resource:'Reporting resource',reporting_return:'Reporting requirement',reporting_template:'Reporting template',reporting_instruction:'Reporting instructions',reporting_source:'Source document',reporting_xbrl_source:'XBRL taxonomy',reporting_datapoint:'Datapoints',reporting_provision:'Referenced provision',reporting_concept:'Reporting concept',ReportingEstate:'Reporting estate',ReportingRegime:'Reporting regime',ReportingCollection:'Reporting collection',ReportingRequirement:'Reporting requirement',RequirementEdition:'Requirement edition',ReportingResource:'Reporting resource',Worksheet:'Worksheet',LogicalTemplate:'Logical template',TaxonomyRelease:'XBRL taxonomy release',DataItem:'Reporting return',Template:'Reporting template',TemplateSet:'XBRL source',InstructionSet:'Reporting instructions',SourceDocument:'Source document',DataPointGroup:'Datapoint summary',DataPoint:'Datapoint',Provision:'Referenced provision'}[v]||String(v||'').replaceAll('_',' '))}
function displayColour(v){return MATERIAL_COLOURS[materialType(v)]||COLOURS.brandMid}
function label(v){return materialLabel(materialType(v))}
function truncate(s='',n=120){return s&&s.length>n?s.slice(0,n-1)+'…':s}
function fmt(v){return typeof v==='number'?v.toLocaleString(undefined,{maximumFractionDigits:3}):v}
function partAudienceCategories(node){
  const categories=node?.metadata?.firm_categories;
  if(!Array.isArray(categories)) return [];
  return categories.map(category=>String(category).replace(/^Non-authorised Persons$/,'Non-authorised persons')).filter(Boolean);
}

const appContainer=document.getElementById('root');
(appContainer.__praRulebookRoot??=createRoot(appContainer)).render(<App/>);
