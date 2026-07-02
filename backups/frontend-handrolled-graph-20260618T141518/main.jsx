import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || '/pra-rulebook-api';
const TYPES = ['contains','references','uses_defined_term','defines','shares_defined_term','has_topic','has_obligation_pattern','has_structured_obligation','shares_obligation_pattern','amends','has_permission'];
const PROVISION_TYPES = ['rule','chapter','guidance_section','guidance_paragraph'];
const NODE_TYPES = [...PROVISION_TYPES,'part','rulebook','defined_term','glossary','crr_terms_list','guidance_document','topic','obligation_pattern','obligation_statement','legal_instrument','permission','external_reference'];
const DEFAULT_TYPES = new Set(['contains','references','uses_defined_term','defines','shares_defined_term','has_topic','has_obligation_pattern','has_structured_obligation','shares_obligation_pattern','amends','has_permission']);
const REPRESENTATIONS = {
  combined: { label:'Combined', hint:'Legal structure plus rolled-up references, terms, obligations and permissions.', types:[...DEFAULT_TYPES], depth:1, explicitOnly:false },
  hierarchy: { label:'Legal hierarchy', hint:'Parts, articles, chapters, rules and paragraphs only.', types:['contains'], depth:2, explicitOnly:false },
  references: { label:'Cross-references', hint:'Explicit and detected cross-reference/amendment links, with child context so Article-level headings expose paragraph-level references.', types:['contains','references','amends'], depth:2, explicitOnly:false },
  definitions: { label:'Definitions', hint:'Definitions, glossary/CRR term usage, and provisions sharing defined terms.', types:['uses_defined_term','defines','shares_defined_term'], depth:2, explicitOnly:false },
  obligations: { label:'Obligations', hint:'Detected obligation statements, obligation patterns, and provisions with similar obligation patterns.', types:['has_obligation_pattern','has_structured_obligation','shares_obligation_pattern'], depth:1, explicitOnly:false },
};
const EXPLICIT = new Set(['site_structure','html_link','html_glossary_link','glossary_source','crr_terms_source','legal_instrument_listing','regex_reference','regex_named_reference','llm_extracted_reference','resolved_part_reference','fca_waivers_list']);
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
  const [panelOpen,setPanelOpen]=useState(()=>window.innerWidth>1400);
  const [graphExpanded,setGraphExpanded]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');

  useEffect(()=>{ bootstrap(); },[]);
  useEffect(()=>{ if(selected && !['whole_map','article_map'].includes(representation)) loadNeighbourhood(selected.id); },[depth,limit,explicitOnly,[...types].sort().join('|')]);

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
    const p=new URLSearchParams({depth:String(depth),limit:String(limit),explicit_only:String(explicitOnly)});
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
    if(selected) loadNeighbourhood(selected.id);
  }
  function toggleType(t){ const next=new Set(types); next.has(t)?next.delete(t):next.add(t); setTypes(next); setRepresentation('custom'); }
  const activeRep=REPRESENTATIONS[representation]||{label:'Custom',hint:'Manual edge-type selection.'};
  const relationshipFilters=useMemo(()=>availableRelationshipTypes(stats,graph),[stats,graph]);
  const visibleGraph=useMemo(()=>filterGraph(graph,nodeTypes,types,originFilter,selected?.id,showInsurance),[graph,nodeTypes,[...types].sort().join('|'),originFilter,selected?.id,showInsurance]);
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

  return <div className={`${graphExpanded?'shell graph-expanded':'shell'} ${panelOpen?'panel-open':'panel-closed'}`}>
    <header className="topbar">
      <a className="home" href="/">‹</a>
      <form className="command" onSubmit={search}>
        <span>⌕</span><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search, or leave blank for all Parts" autoFocus/><button>{busy?'…':'Search'}</button>
      </form>
      <div className="top-actions">
        <button onClick={()=>setPanelOpen(!panelOpen)} title="Toggle side panel">◧</button>
        <details className="settings"><summary title="Display settings">⚙</summary><div className="settings-pop">
          <div className="filter-section representation-section"><h4>Representation</h4><div className="type-grid representation-grid">{Object.entries(REPRESENTATIONS).map(([key,preset])=><button type="button" key={key} className={representation===key?'on':''} onClick={()=>applyRepresentation(key)}><span>{preset.label}</span></button>)}<button type="button" className={representation==='custom'?'on':''} onClick={()=>applyRepresentation('custom')}><span>Custom</span></button></div><p className="rep-hint"><b>{activeRep.label}</b>{activeRep.hint}</p></div>
          <label>Depth <input type="range" min="1" max="3" value={depth} onChange={e=>{setDepth(Number(e.target.value));setRepresentation('custom')}}/><b>{depth}</b></label>
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
      <div className="result-stack">{results.map(r=><button key={r.id} className={selected?.id===r.id?'hit active':'hit'} onClick={()=>choose(r)}><span>{label(r.node_type)}</span><strong>{displayNodeTitle(r)}</strong><small>{truncate(r.snippet||r.text,128)}</small></button>)}</div>
    </aside>

    <main className="canvas">
      <div className="canvas-meta"><strong>{selected?.title||'Select a node'}</strong><span>{activeRep.label} · {visibleGraph.nodes.length} shown · {visibleGraph.edges.length} visible links · {Object.values(graph.available_edge_types||{}).reduce((a,b)=>a+b,0)} direct links available</span><button className="expand-graph" onClick={()=>setGraphExpanded(v=>!v)}>{graphExpanded?'Collapse graph':'Expand graph'}</button></div>
      <Graph graph={visibleGraph} selected={selected} detail={detail} nodeTypes={nodeTypes} relationshipTypes={types} relationshipFilters={relationshipFilters} availableEdgeTypes={graph.available_edge_types||{}} onToggleNodeType={toggleNodeType} onToggleRelationship={toggleType} onSelect={n=>{setDetail(n);setPanelOpen(true);}} onOpen={n=>choose(n,{drill:true})}/>
    </main>

    <aside className={panelOpen?'inspector open':'inspector'}>
      <Explore node={detail} edges={selectedEdges} graph={graph} onChoose={choose}/>
    </aside>
  </div>;
}

function Graph({graph,selected,detail,nodeTypes,relationshipTypes,relationshipFilters,availableEdgeTypes,onToggleNodeType,onToggleRelationship,onSelect,onOpen}){
  const [view,setView]=useState({x:0,y:0,z:1});
  const [drag,setDrag]=useState(null);
  const [hover,setHover]=useState(null);
  const svgRef=useRef(null);
  const {nodes,edges}=useMemo(()=>layout(graph,selected?.id),[graph,selected?.id]);
  const byId=new Map(nodes.map(n=>[n.id,n]));
  useEffect(()=>{ if(nodes.length) setView(fitView(nodes)); },[graph.level, selected?.id, nodes.length]);
  useEffect(()=>{
    function keys(e){
      if(e.target?.tagName==='INPUT') return;
      if(e.key==='0'){e.preventDefault(); setView(fitView(nodes));}
      if(e.key==='f' && detail){e.preventDefault(); focusNode(detail);}
      if(e.key==='+' || e.key==='='){e.preventDefault(); zoomAtCentre(1.18);}
      if(e.key==='-'){e.preventDefault(); zoomAtCentre(.86);}
      if(['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)){
        e.preventDefault();
        const step=e.shiftKey?80:32;
        setView(v=>({...v,x:v.x+(e.key==='ArrowRight'?-step:e.key==='ArrowLeft'?step:0),y:v.y+(e.key==='ArrowDown'?-step:e.key==='ArrowUp'?step:0)}));
      }
    }
    window.addEventListener('keydown',keys);
    return ()=>window.removeEventListener('keydown',keys);
  },[nodes,detail]);
  function clampZoom(z){return Math.max(.35,Math.min(4,z));}
  function startPan(e){ if(e.target.closest('.node')) return; e.currentTarget.setPointerCapture?.(e.pointerId); setDrag({sx:e.clientX,sy:e.clientY,x:view.x,y:view.y}); }
  function movePan(e){ if(!drag) return; setView(v=>({...v,x:drag.x+(e.clientX-drag.sx),y:drag.y+(e.clientY-drag.sy)})); }
  function endPan(){ setDrag(null); }
  function zoomAtCentre(mult){ setView(v=>({x:600-(600-v.x)*clampZoom(v.z*mult)/v.z,y:410-(410-v.y)*clampZoom(v.z*mult)/v.z,z:clampZoom(v.z*mult)})); }
  function zoomAtPointer(e){
    e.preventDefault();
    const rect=svgRef.current?.getBoundingClientRect(); if(!rect) return;
    const px=(e.clientX-rect.left)*1200/rect.width, py=(e.clientY-rect.top)*820/rect.height;
    const mult=e.deltaY>0?.88:1.14;
    setView(v=>{const nz=clampZoom(v.z*mult); return {x:px-(px-v.x)*nz/v.z,y:py-(py-v.y)*nz/v.z,z:nz};});
  }
  function focusNode(n){ if(!n) return; setView(v=>({x:600-n.x*Math.max(1.15,v.z),y:410-n.y*Math.max(1.15,v.z),z:Math.max(1.15,v.z)})); }
  function fitAll(){ setView(fitView(nodes)); }
  return <div className="graph-wrap">
    <svg ref={svgRef} className={drag?'graph panning':'graph'} viewBox="0 0 1200 820"
      onPointerDown={startPan} onPointerMove={movePan} onPointerUp={endPan} onPointerLeave={()=>{endPan();setHover(null)}}
      onWheel={zoomAtPointer}>
      <g transform={`translate(${view.x} ${view.y}) scale(${view.z})`}>
        {edges.map(e=>{const a=byId.get(e.from_node_id),b=byId.get(e.to_node_id); if(!a||!b)return null; const inf=isInferred(e); return <g key={e.id} className={inf?'edge-group inferred':'edge-group'}><line x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={`edge edge-${e.edge_type}`} style={{stroke:e.visual?.colour||edgeColour(e.edge_type),strokeDasharray:inf?'5 7':(e.visual?.line_style==='dashed'?'5 7':undefined)}} strokeWidth={e.visual?.width||Math.max(1.2,(e.confidence||.45)*2.8)} /></g>})}
        {nodes.map(n=><g key={n.id} className={`node ${selected?.id===n.id?'selected':''} ${detail?.id===n.id?'focus':''}`}
          onClick={()=>onSelect(n)} onDoubleClick={()=>onOpen(n)}
          onPointerEnter={e=>setHover({node:n,x:e.clientX,y:e.clientY})}
          onPointerMove={e=>setHover({node:n,x:e.clientX,y:e.clientY})}
          onPointerLeave={()=>setHover(null)}>
          <circle cx={n.x} cy={n.y} r={r(n,graph)} fill={nodeFill(n,graph)} />
          {showNodeLabel(n,view,graph,selected)&&<text x={n.x} y={n.y+r(n,graph)+labelOffset(view)} textAnchor="middle" fontSize={labelSize(view,graph)}>{truncate(displayNodeTitle(n),labelChars(n,view,graph,selected))}</text>}
        </g>)}
      </g>
    </svg>
    {hover&&<div className="node-tip" style={{left:hover.x+14,top:hover.y+14}}><span>{materialLabel(materialType(hover.node))}</span><strong>{displayNodeTitle(hover.node)}</strong><small>{truncate(hover.node.text||hover.node.url||'',180)}</small></div>}
    <Legend active={nodeTypes} relationshipTypes={relationshipTypes} relationshipFilters={relationshipFilters} availableEdgeTypes={availableEdgeTypes} onToggle={onToggleNodeType} onToggleRelationship={onToggleRelationship} />
    <div className="zoom"><button title="Zoom in (+)" onClick={()=>zoomAtCentre(1.18)}>＋</button><button title="Zoom out (-)" onClick={()=>zoomAtCentre(.86)}>−</button><button title="Fit graph (0)" onClick={fitAll}>⤢</button><button title="Focus selected (F)" onClick={()=>focusNode(detail||selected)}>◎</button></div>
    <div className="nav-help">Wheel zooms to cursor · drag pans · 0 fits · F focuses · arrows pan</div>
  </div>;
}

function Legend({active,relationshipTypes,relationshipFilters,availableEdgeTypes,onToggle,onToggleRelationship}){
  return <div className="legend split"><div className="legend-title">Material</div>{MATERIAL_FILTERS.map(t=><button key={t} className={materialFilterOn(t,active)?'on':'off'} onClick={()=>onToggle(t)} title={`Toggle ${materialLabel(t)}`}><i style={{background:MATERIAL_COLOURS[t]||'#64748b'}} /> <span>{materialLabel(t)}</span></button>)}<div className="legend-title">Relationship edges</div>{relationshipFilters.map(t=><button key={t} className={relationshipTypes?.has(t)?'on':'off'} onClick={()=>onToggleRelationship(t)} title={`Toggle ${relationLabel(t)}`}><i className="line" style={{borderTopColor:edgeColour(t)}}/> <span>{relationLabel(t)}</span>{availableEdgeTypes?.[t]>0&&<em>{availableEdgeTypes[t]}</em>}</button>)}<div><i className="line dash"/> <span>inferred / derived link</span></div></div>
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
      <span className="content-body"><span className="content-meta"><b>child</b><em>{label(node.node_type)}</em>{number&&<em>{number}</em>}{kids.length>0&&<em>{kids.length} item{kids.length===1?'':'s'}</em>}</span><strong>{displayNodeTitle(node)}</strong>{node.text&&<small>{truncate(node.text,190)}</small>}</span>
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
      <span className="kind">{label(node.node_type)}</span><h2>{displayNodeTitle(node)}</h2>{node.url&&<a className="source" href={node.url} target="_blank">Open source ↗</a>}
      <p className="text">{node.text?truncate(node.text,1300):emptyNodeMessage(node)}</p>
    </Collapsible>
    {groups.length
      ? groups.map(([edgeType,items],i)=><Collapsible key={edgeType} title={evidenceLabel(edgeType)} count={`${items.length} link${items.length===1?'':'s'}`} open={i<2}>
          <div className="edge-list">{items.slice(0,40).map(e=>{const other=byId.get(e.from_node_id===node.id?e.to_node_id:e.from_node_id);return <button key={e.id} onClick={()=>other&&onChoose(other)}><span>{edgeSummary(e)}</span><strong>{edgeNodeTitle(other,e,node)}</strong>{edgeContext(e,other)&&<small>{edgeContext(e,other)}</small>}{e.evidence_text&&<small>{truncate(e.evidence_text,160)}</small>}</button>})}</div>
          {items.length>40&&<p className="muted">Showing first 40 of {items.length} visible links. Increase the graph cap to load more.</p>}
        </Collapsible>)
      : <Collapsible title="Visible connections" count="0 links" open><p className="muted">No reference, definition, topic or obligation links are visible for this node under the current settings.</p></Collapsible>}
  </section>;
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

function isInsuranceNode(n){
  const hay=[n.title,n.text,n.url,n.metadata?.part_title,n.metadata?.document_title,n.metadata?.topic].filter(Boolean).join(' ').toLowerCase();
  return /insurance|insurer|solvency ii|sii|non-solvency|policyholder|with-profits|actuar|matching adjustment|technical provisions|own funds/.test(hay);
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
function edgeSummary(e){
  const confidence=`${Math.round((e.confidence||0)*100)}%`;
  return `${relationLabel(e.edge_type)} · ${provenanceLabel(e.source_method)} · ${confidence}`;
}
function edgeNodeTitle(node,e,current){
  return displayNodeTitle(node);
}
function displayNodeTitle(node){
  if(!node) return 'Unloaded node';
  const title=node.title||'Untitled node';
  if(/^article\b/i.test(title)) return title;
  const part=node.metadata?.part_title||node.metadata?.document_title;
  if(part && /^\d/.test(title) && !title.startsWith(part)) return `${part} ${title}`;
  return title;
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
