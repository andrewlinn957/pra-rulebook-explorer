import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || '/pra-rulebook-api';
const TYPES = ['contains','references','uses_defined_term','defines','similar_to','has_topic','has_obligation_pattern','shares_obligation_pattern','amends','resolves_to_part'];
const PROVISION_TYPES = ['rule','chapter','guidance_section','guidance_paragraph'];
const NODE_TYPES = [...PROVISION_TYPES,'part','rulebook','defined_term','glossary','crr_terms_list','guidance_document','topic','topic_cluster','obligation_pattern','obligation_statement','legal_instrument','external_reference','rule_reference'];
const DEFAULT_TYPES = new Set(['contains','references','uses_defined_term','defines','similar_to','has_topic','shares_obligation_pattern','amends','resolves_to_part']);
const REPRESENTATIONS = {
  combined: { label:'Combined', hint:'Legal structure plus rolled-up references, terms, semantic links and obligations.', types:[...DEFAULT_TYPES], depth:1, explicitOnly:false },
  hierarchy: { label:'Legal hierarchy', hint:'Parts, articles, chapters, rules and paragraphs only.', types:['contains'], depth:2, explicitOnly:false },
  references: { label:'Cross-references', hint:'Rolled-up cross-reference links, with child context so Article-level headings expose paragraph-level references.', types:['contains','references','amends','resolves_to_part'], depth:2, explicitOnly:false },
  definitions: { label:'Definitions', hint:'Rolled-up glossary and CRR term usage.', types:['uses_defined_term','defines'], depth:2, explicitOnly:false },
  semantic: { label:'Semantic similarity', hint:'Rolled-up embedding-derived similarity and topic links.', types:['similar_to','has_topic'], depth:1, explicitOnly:false },
  obligations: { label:'Obligations', hint:'Rolled-up provisions with similar obligation patterns.', types:['has_obligation_pattern','shares_obligation_pattern'], depth:1, explicitOnly:false },
  whole_map: { label:'Whole Rulebook map', hint:'Part-level semantic map: distance reflects averaged provision embeddings, colour shows topic clusters, size reflects weighted connections.', mapLevel:'part' },
  article_map: { label:'Article semantic map', hint:'Article/chapter-level semantic map for zooming into the Rulebook structure.', mapLevel:'article' },
};
const EXPLICIT = new Set(['site_structure','html_link','html_glossary_link','glossary_source','crr_terms_source','legal_instrument_listing','regex_reference','regex_named_reference']);
const RELATION_LABELS = { contains:'contains / child', references:'cross-reference', uses_defined_term:'uses defined term', defines:'defines', similar_to:'semantic similarity', has_topic:'has topic', has_topic_cluster:'topic cluster', has_obligation_pattern:'obligation signal', shares_obligation_pattern:'similar obligation', has_structured_obligation:'structured obligation', amends:'amends', resolves_to_part:'resolved Part reference' };
const EDGE_COLOURS = { contains:'#94a3b8', references:'#60a5fa', uses_defined_term:'#f59e0b', defines:'#fbbf24', similar_to:'#d7ff64', has_topic:'#d35cff', has_topic_cluster:'#a78bfa', has_obligation_pattern:'#fb7185', shares_obligation_pattern:'#f97316', has_structured_obligation:'#f43f5e', amends:'#ef4444', resolves_to_part:'#38bdf8' };
const MATERIAL_COLOURS = { rule:'#4f7cff', supervisory_statement:'#22c55e', statement_of_policy:'#14b8a6', definition:'#d28b24', external_reference:'#7b8190', legal_instrument:'#cc5c5c', analysis:'#d35cff', rulebook:'#9b6bff' };
const CLUSTER_COLOURS = ['#4f7cff','#d28b24','#58a978','#d35cff','#cc5c5c','#35b6b4','#d7ff64','#a78bfa','#fb7185','#60a5fa','#f59e0b','#34d399'];
const MATERIAL_FILTERS = ['rule','supervisory_statement','statement_of_policy','definition','legal_instrument','external_reference'];
const RELATIONSHIP_FILTERS = TYPES;

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
  const [types,setTypes]=useState(DEFAULT_TYPES);
  const [nodeTypes,setNodeTypes]=useState(new Set(NODE_TYPES));
  const [showInsurance,setShowInsurance]=useState(false);
  const [stats,setStats]=useState(null);
  const [panelOpen,setPanelOpen]=useState(true);
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
      if(roots.results?.[0]) await choose(roots.results[0], {drill:false});
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
    setSelected(full); setDetail(full); setPanelOpen(true);
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
  async function loadWholeMap(level='part'){
    setBusy(true); setError('');
    try{
      const data=await api(`/analysis/semantic-map?level=${level}&clusters=${level==='article'?18:12}&edge_limit=${level==='article'?1800:700}`);
      setGraph(data);
      setRailContext({kind:'Map',title:level==='article'?'Article semantic map':'Whole Rulebook'});
    }catch(err){setError(err.message||String(err));}
    finally{setBusy(false);}
  }
  function applyRepresentation(key){
    if(key==='custom'){ setRepresentation('custom'); return; }
    const preset=REPRESENTATIONS[key]||REPRESENTATIONS.combined;
    setRepresentation(key);
    if(key==='whole_map' || key==='article_map'){ loadWholeMap(preset.mapLevel||'part'); return; }
    setTypes(new Set(preset.types));
    setDepth(preset.depth);
    setExplicitOnly(preset.explicitOnly);
    if(selected) loadNeighbourhood(selected.id);
  }
  function toggleType(t){ const next=new Set(types); next.has(t)?next.delete(t):next.add(t); setTypes(next); setRepresentation('custom'); }
  const activeRep=REPRESENTATIONS[representation]||{label:'Custom',hint:'Manual edge-type selection.'};
  const visibleGraph=useMemo(()=>filterGraph(graph,nodeTypes,types,selected?.id,showInsurance),[graph,nodeTypes,[...types].sort().join('|'),selected?.id,showInsurance]);
  const selectedEdges=useMemo(()=>visibleGraph.edges.filter(e=>detail&&(e.from_node_id===detail.id||e.to_node_id===detail.id)),[visibleGraph,detail]);
  function toggleNodeType(t){
    const next=new Set(nodeTypes);
    const groups={
      rule:['rule','chapter','part','rulebook'],
      definition:['defined_term','glossary','crr_terms_list'],
      supervisory_statement:['guidance_document','guidance_section','guidance_paragraph'],
      statement_of_policy:['guidance_document','guidance_section','guidance_paragraph'],
      analysis:['topic','topic_cluster','obligation_pattern','obligation_statement'],
      legal_instrument:['legal_instrument'],
      external_reference:['external_reference','rule_reference'],
    };
    const group=groups[t]||[t];
    const allOn=group.every(x=>next.has(x));
    group.forEach(x=>allOn?next.delete(x):next.add(x));
    setNodeTypes(next);
  }

  return <div className={graphExpanded?'shell graph-expanded':'shell'}>
    <header className="topbar">
      <a className="home" href="/">‹</a>
      <form className="command" onSubmit={search}>
        <span>⌕</span><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search, or leave blank for all Parts" autoFocus/><button>{busy?'…':'Search'}</button>
      </form>
      <label className="representation"><span>Representation</span><select value={representation} onChange={e=>applyRepresentation(e.target.value)}><option value="combined">Combined</option><option value="whole_map">Whole Rulebook map</option><option value="article_map">Article semantic map</option><option value="hierarchy">Legal hierarchy</option><option value="references">Cross-references</option><option value="definitions">Definitions</option><option value="semantic">Semantic similarity</option><option value="obligations">Obligations</option><option value="custom">Custom</option></select></label>
      <div className="top-actions">
        <button onClick={()=>setPanelOpen(!panelOpen)} title="Toggle side panel">◧</button>
        <details className="settings"><summary title="Display settings">⚙</summary><div className="settings-pop">
          <p className="rep-hint"><b>{activeRep.label}</b>{activeRep.hint}</p>
          <label>Depth <input type="range" min="1" max="3" value={depth} onChange={e=>{setDepth(Number(e.target.value));setRepresentation('custom')}}/><b>{depth}</b></label>
          <label>Node cap <input type="number" min="30" max="800" value={limit} onChange={e=>setLimit(Number(e.target.value))}/></label>
          <label className="check"><input type="checkbox" checked={explicitOnly} onChange={e=>{setExplicitOnly(e.target.checked);setRepresentation('custom')}}/> Explicit only</label>
          <label className="check"><input type="checkbox" checked={showInsurance} onChange={e=>setShowInsurance(e.target.checked)}/> Insurance parts</label>
          <div className="filter-section"><h4>Material</h4><div className="type-grid material-grid">{MATERIAL_FILTERS.map(t=><button type="button" key={t} className={materialFilterOn(t,nodeTypes)?'on':''} onClick={()=>toggleNodeType(t)}><span>{materialLabel(t)}</span></button>)}</div></div>
          <div className="filter-section"><h4>Relationship</h4><div className="type-grid">{RELATIONSHIP_FILTERS.map(t=><button type="button" key={t} className={types.has(t)?'on':''} onClick={()=>toggleType(t)}><span>{relationLabel(t)}</span><em>{graph.available_edge_types?.[t]||0}</em></button>)}</div></div>
        </div></details>
      </div>
    </header>

    <aside className="rail">
      <div className="product"><strong>PRA Rulebook</strong><span>{railContext?`${railContext.kind} · ${railContext.title}`:(q.trim()?'Search results':'All Rulebook Parts')} · {stats?`${stats.nodes.toLocaleString()} nodes`:''}</span><div className="rail-actions">{railStack.length>0&&<button className="back-link" onClick={goUp}>‹ Up one level</button>}{railContext&&<button className="back-link secondary" onClick={loadAllParts}>All Parts</button>}</div></div>
      {error&&<div className="error">{error}</div>}
      <div className="result-stack">{results.map(r=><button key={r.id} className={selected?.id===r.id?'hit active':'hit'} onClick={()=>choose(r)}><span>{label(r.node_type)}</span><strong>{r.title}</strong><small>{truncate(r.snippet||r.text,128)}</small></button>)}</div>
    </aside>

    <main className="canvas">
      <div className="canvas-meta"><strong>{selected?.title||'Select a node'}</strong><span>{activeRep.label} · {visibleGraph.nodes.length} shown · {visibleGraph.edges.length} visible links · {Object.values(graph.available_edge_types||{}).reduce((a,b)=>a+b,0)} direct links available</span><button className="expand-graph" onClick={()=>setGraphExpanded(v=>!v)}>{graphExpanded?'Collapse graph':'Expand graph'}</button></div>
      <Graph graph={visibleGraph} selected={selected} detail={detail} nodeTypes={nodeTypes} onToggleNodeType={toggleNodeType} onSelect={n=>{setDetail(n);setPanelOpen(true);}} onOpen={n=>choose(n,{drill:true})}/>
    </main>

    <aside className={panelOpen?'inspector open':'inspector'}>
      <Explore node={detail} edges={selectedEdges} graph={graph} onChoose={choose}/>
    </aside>
  </div>;
}

function Graph({graph,selected,detail,nodeTypes,onToggleNodeType,onSelect,onOpen}){
  const [view,setView]=useState({x:0,y:0,z:1});
  const [drag,setDrag]=useState(null);
  const [hover,setHover]=useState(null);
  const svgRef=useRef(null);
  const {nodes,edges}=useMemo(()=>layout(graph,selected?.id),[graph,selected?.id]);
  const byId=new Map(nodes.map(n=>[n.id,n]));
  function startPan(e){ if(e.target.closest('.node')) return; setDrag({sx:e.clientX,sy:e.clientY,x:view.x,y:view.y}); }
  function movePan(e){ if(!drag) return; setView(v=>({...v,x:drag.x+(e.clientX-drag.sx),y:drag.y+(e.clientY-drag.sy)})); }
  function endPan(){ setDrag(null); }
  return <div className="graph-wrap">
    <svg ref={svgRef} className={`${drag?'graph panning':'graph'} ${graph.level==='part'?'semantic-map':''}`} viewBox="0 0 1200 820"
      onPointerDown={startPan} onPointerMove={movePan} onPointerUp={endPan} onPointerLeave={()=>{endPan();setHover(null)}}
      onWheel={e=>{e.preventDefault(); const dz=e.deltaY>0?.92:1.08; setView(v=>({...v,z:Math.max(.55,Math.min(1.8,v.z*dz))}));}}>
      <g transform={`translate(${view.x} ${view.y}) scale(${view.z})`}>
        {edges.map(e=>{const a=byId.get(e.from_node_id),b=byId.get(e.to_node_id); if(!a||!b)return null; const inf=!EXPLICIT.has(e.source_method); return <g key={e.id} className={inf?'edge-group inferred':'edge-group'}><line x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={`edge edge-${e.edge_type}`} style={{stroke:e.visual?.colour||edgeColour(e.edge_type),strokeDasharray:e.visual?.line_style==='dashed'?'5 7':undefined}} strokeWidth={e.visual?.width||Math.max(1.2,(e.confidence||.45)*2.8)} /></g>})}
        {nodes.map(n=><g key={n.id} className={`node ${selected?.id===n.id?'selected':''} ${detail?.id===n.id?'focus':''}`}
          onClick={()=>onSelect(n)} onDoubleClick={()=>onOpen(n)}
          onPointerEnter={e=>setHover({node:n,x:e.clientX,y:e.clientY})}
          onPointerMove={e=>setHover({node:n,x:e.clientX,y:e.clientY})}
          onPointerLeave={()=>setHover(null)}>
          <circle cx={n.x} cy={n.y} r={r(n,graph)} fill={nodeFill(n,graph)} />
          {showNodeLabel(n,view,graph,selected)&&<text x={n.x} y={n.y+r(n,graph)+labelOffset(view)} textAnchor="middle" fontSize={labelSize(view,graph)}>{truncate(n.title||n.id,labelChars(n,view,graph,selected))}</text>}
        </g>)}
      </g>
    </svg>
    {hover&&<div className="node-tip" style={{left:hover.x+14,top:hover.y+14}}><span>{materialLabel(materialType(hover.node))}</span><strong>{hover.node.title}</strong><small>{truncate(hover.node.text||hover.node.url||'',180)}</small></div>}
    <Legend active={nodeTypes} onToggle={onToggleNodeType} />
    <div className="zoom"><button onClick={()=>setView(v=>({...v,z:Math.min(1.8,v.z*1.15)}))}>＋</button><button onClick={()=>setView(v=>({...v,z:Math.max(.55,v.z*.85)}))}>−</button><button onClick={()=>setView({x:0,y:0,z:1})}>⌂</button></div>
  </div>;
}

function Legend({active,onToggle}){
  return <div className="legend split"><div className="legend-title">Material</div>{MATERIAL_FILTERS.map(t=><button key={t} className={materialFilterOn(t,active)?'on':'off'} onClick={()=>onToggle(t)} title={`Toggle ${materialLabel(t)}`}><i style={{background:MATERIAL_COLOURS[t]||'#64748b'}} /> <span>{materialLabel(t)}</span></button>)}<div className="legend-title">Relationship</div>{['contains','references','uses_defined_term','similar_to','has_topic','has_obligation_pattern','shares_obligation_pattern','amends','resolves_to_part'].map(t=><div key={t}><i className="line" style={{borderTopColor:edgeColour(t)}}/> <span>{relationLabel(t)}</span></div>)}<div><i className="line dash"/> <span>computed link</span></div></div>
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
      <span className="content-body"><span className="content-meta"><b>child</b><em>{label(node.node_type)}</em>{number&&<em>{number}</em>}{kids.length>0&&<em>{kids.length} item{kids.length===1?'':'s'}</em>}</span><strong>{node.title}</strong>{node.text&&<small>{truncate(node.text,190)}</small>}</span>
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
  return <section className="explore-layer evidence-layer" aria-label="Analytical evidence">
    <div className="layer-head"><span>Analytical evidence</span><h3>Selected node</h3></div>
    <Collapsible title="Selected provision" count={label(node.node_type)} open>
      <span className="kind">{label(node.node_type)}</span><h2>{node.title}</h2>{node.url&&<a className="source" href={node.url} target="_blank">Open source ↗</a>}
      <p className="text">{node.text?truncate(node.text,1300):emptyNodeMessage(node)}</p>
    </Collapsible>
    {groups.length
      ? groups.map(([edgeType,items],i)=><Collapsible key={edgeType} title={relationLabel(edgeType)} count={`${items.length} link${items.length===1?'':'s'}`} open={i<2}>
          <div className="edge-list">{items.slice(0,40).map(e=>{const other=byId.get(e.from_node_id===node.id?e.to_node_id:e.from_node_id);return <button key={e.id} onClick={()=>other&&onChoose(other)}><span>{relationLabel(e.edge_type)} · {e.source_method} · {Math.round((e.confidence||0)*100)}%</span><strong>{other?.title||'Unloaded node'}</strong>{e.evidence_text&&<small>{truncate(e.evidence_text,160)}</small>}</button>})}</div>
          {items.length>40&&<p className="muted">Showing first 40 of {items.length} visible links. Increase the graph cap to load more.</p>}
        </Collapsible>)
      : <Collapsible title="Visible analytical links" count="0 links" open><p className="muted">No semantic, reference, definition or obligation links are visible for this node under the current representation/settings.</p></Collapsible>}
  </section>;
}
function Collapsible({title,count,open=false,children}){
  return <details className="collapse-card" open={open}><summary><span>{title}</span>{count&&<em>{count}</em>}</summary><div className="collapse-body">{children}</div></details>;
}
function groupEdges(edges){
  const priority=['references','uses_defined_term','defines','similar_to','has_topic','has_topic_cluster','has_obligation_pattern','shares_obligation_pattern','has_structured_obligation','amends','resolves_to_part'];
  const buckets=new Map();
  for(const e of edges) buckets.set(e.edge_type,[...(buckets.get(e.edge_type)||[]),e]);
  return [...buckets.entries()].sort((a,b)=>{
    const ai=priority.indexOf(a[0]), bi=priority.indexOf(b[0]);
    return (ai<0?99:ai)-(bi<0?99:bi) || b[1].length-a[1].length || a[0].localeCompare(b[0]);
  });
}
function filterGraph(graph,nodeTypes,relationshipTypes,selectedId,showInsurance=true){
  const keepNodes=(graph.nodes||[]).filter(n=>(nodeTypes.has(n.node_type)||n.id===selectedId) && (showInsurance || n.id===selectedId || !isInsuranceNode(n)));
  const keepIds=new Set(keepNodes.map(n=>n.id));
  return {...graph,nodes:keepNodes,edges:(graph.edges||[]).filter(e=>keepIds.has(e.from_node_id)&&keepIds.has(e.to_node_id)&&(!relationshipTypes?.size||relationshipTypes.has(e.edge_type)))};
}

function isInsuranceNode(n){
  const hay=[n.title,n.text,n.url,n.metadata?.part_title,n.metadata?.document_title,n.metadata?.topic].filter(Boolean).join(' ').toLowerCase();
  return /insurance|insurer|solvency ii|sii|non-solvency|policyholder|with-profits|actuar|matching adjustment|technical provisions|own funds/.test(hay);
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
function materialFilterOn(t,active){
  const groups={
    rule:['rule','chapter','part','rulebook'],
    definition:['defined_term','glossary','crr_terms_list'],
    supervisory_statement:['guidance_document','guidance_section','guidance_paragraph'],
    statement_of_policy:['guidance_document','guidance_section','guidance_paragraph'],
    legal_instrument:['legal_instrument'],
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
  if(type==='external_reference' || type==='rule_reference') return 'external_reference';
  if(['topic','topic_cluster','obligation_pattern','obligation_statement'].includes(type)) return 'analysis';
  if(['guidance_document','guidance_section','guidance_paragraph'].includes(type)){
    if(doc.includes('statement_of_policy') || url.includes('/statements-of-policy/')) return 'statement_of_policy';
    return 'supervisory_statement';
  }
  return type||'external_reference';
}
function materialLabel(v){return ({rule:'Rulebook part / rule',supervisory_statement:'Supervisory statement',statement_of_policy:'Statement of policy',definition:'Definition',external_reference:'External reference',legal_instrument:'Legal instrument',analysis:'Topic / obligation marker'}[v]||String(v||'').replaceAll('_',' '))}
function displayColour(v){return MATERIAL_COLOURS[materialType(v)]||'#64748b'}
function label(v){return materialLabel(materialType(v))}
function truncate(s='',n=120){return s&&s.length>n?s.slice(0,n-1)+'…':s}

createRoot(document.getElementById('root')).render(<App/>);
