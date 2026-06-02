import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || '/pra-rulebook-api';
const TYPES = ['contains','references','uses_defined_term','defines','similar_to','has_topic','has_obligation_pattern','shares_obligation_pattern','amends','resolves_to_part'];
const NODE_TYPES = ['rule','chapter','part','defined_term','guidance_document','guidance_section','guidance_paragraph','topic','obligation_pattern','legal_instrument','external_reference'];
const DEFAULT_TYPES = new Set(['contains','references','uses_defined_term','defines','similar_to','has_topic','shares_obligation_pattern','amends','resolves_to_part']);
const REPRESENTATIONS = {
  combined: { label:'Combined', hint:'Legal structure plus references, terms, semantic links and obligations.', types:[...DEFAULT_TYPES], depth:1, explicitOnly:false },
  hierarchy: { label:'Legal hierarchy', hint:'Parts, articles, chapters, rules and paragraphs only.', types:['contains'], depth:2, explicitOnly:true },
  references: { label:'Cross-references', hint:'Explicit legal and named references between provisions.', types:['references','amends','resolves_to_part'], depth:2, explicitOnly:true },
  definitions: { label:'Definitions', hint:'Glossary and CRR term usage.', types:['uses_defined_term','defines'], depth:2, explicitOnly:true },
  semantic: { label:'Semantic similarity', hint:'Embedding-derived similarity and topic links.', types:['similar_to','has_topic'], depth:1, explicitOnly:false },
  obligations: { label:'Obligations', hint:'Provisions with similar obligation patterns.', types:['has_obligation_pattern','shares_obligation_pattern'], depth:1, explicitOnly:false },
};
const EXPLICIT = new Set(['site_structure','html_link','html_glossary_link','glossary_source','crr_terms_source','legal_instrument_listing','regex_reference','regex_named_reference']);
const COLOUR = { rule:'#4f7cff', part:'#9b6bff', chapter:'#738195', defined_term:'#d28b24', guidance_document:'#2d9b63', guidance_section:'#58a978', guidance_paragraph:'#80b98e', legal_instrument:'#cc5c5c', topic:'#d35cff', obligation_pattern:'#e06f2d', external_reference:'#7b8190', rulebook:'#111827' };

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
  const [sideTab,setSideTab]=useState('explore');
  const [analysis,setAnalysis]=useState({centrality:[],bridges:[],interesting:[],stats:null});
  const [panelOpen,setPanelOpen]=useState(true);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');

  useEffect(()=>{ bootstrap(); },[]);
  useEffect(()=>{ if(selected) loadNeighbourhood(selected.id); },[depth,limit,explicitOnly,[...types].sort().join('|')]);

  async function api(path){
    const r=await fetch(API_BASE+path);
    if(!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
    return r.json();
  }
  async function bootstrap(){
    try{
      const [stats,interesting,centrality,bridges,parts,roots]=await Promise.all([
        api('/stats'), api('/interesting?limit=10'), api('/centrality?limit=10'), api('/analysis/betweenness?limit=8&k=60&max_nodes=800'),
        api('/nodes?types=part&limit=300'), api('/nodes?types=rulebook&limit=1')
      ]);
      setAnalysis({stats,interesting:interesting.results||[],centrality:centrality.degree||[],bridges:bridges.results||[]});
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
    const [tree]=await Promise.all([loadContents(full.id), loadNeighbourhood(full.id)]);
    if(opts.drill!==false && tree?.children?.length && ['rulebook','part','chapter'].includes(full.node_type)){
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
  }
  function toggleType(t){ const next=new Set(types); next.has(t)?next.delete(t):next.add(t); setTypes(next); setRepresentation('custom'); }
  const activeRep=REPRESENTATIONS[representation]||{label:'Custom',hint:'Manual edge-type selection.'};
  const visibleGraph=useMemo(()=>filterGraph(graph,nodeTypes,selected?.id,showInsurance),[graph,nodeTypes,selected?.id,showInsurance]);
  const selectedEdges=useMemo(()=>visibleGraph.edges.filter(e=>detail&&(e.from_node_id===detail.id||e.to_node_id===detail.id)),[visibleGraph,detail]);
  function toggleNodeType(t){ const next=new Set(nodeTypes); next.has(t)?next.delete(t):next.add(t); setNodeTypes(next); }

  return <div className="shell">
    <header className="topbar">
      <a className="home" href="/">‹</a>
      <form className="command" onSubmit={search}>
        <span>⌕</span><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search, or leave blank for all Parts" autoFocus/><button>{busy?'…':'Search'}</button>
      </form>
      <label className="representation"><span>Representation</span><select value={representation} onChange={e=>applyRepresentation(e.target.value)}><option value="combined">Combined</option><option value="hierarchy">Legal hierarchy</option><option value="references">Cross-references</option><option value="definitions">Definitions</option><option value="semantic">Semantic similarity</option><option value="obligations">Obligations</option><option value="custom">Custom</option></select></label>
      <div className="top-actions">
        <button onClick={()=>setPanelOpen(!panelOpen)} title="Toggle side panel">◧</button>
        <details className="settings"><summary title="Display settings">⚙</summary><div className="settings-pop">
          <p className="rep-hint"><b>{activeRep.label}</b>{activeRep.hint}</p>
          <label>Depth <input type="range" min="1" max="3" value={depth} onChange={e=>{setDepth(Number(e.target.value));setRepresentation('custom')}}/><b>{depth}</b></label>
          <label>Node cap <input type="number" min="30" max="800" value={limit} onChange={e=>setLimit(Number(e.target.value))}/></label>
          <label className="check"><input type="checkbox" checked={explicitOnly} onChange={e=>{setExplicitOnly(e.target.checked);setRepresentation('custom')}}/> Explicit only</label>
          <label className="check"><input type="checkbox" checked={showInsurance} onChange={e=>setShowInsurance(e.target.checked)}/> Insurance parts</label>
          <div className="type-grid">{TYPES.map(t=><button type="button" key={t} className={types.has(t)?'on':''} onClick={()=>toggleType(t)}><span>{t.replaceAll('_',' ')}</span><em>{graph.available_edge_types?.[t]||0}</em></button>)}</div>
        </div></details>
      </div>
    </header>

    <aside className="rail">
      <div className="product"><strong>PRA Rulebook</strong><span>{railContext?`${railContext.kind} · ${railContext.title}`:(q.trim()?'Search results':'All Rulebook Parts')} · {analysis.stats?`${analysis.stats.nodes.toLocaleString()} nodes`:''}</span><div className="rail-actions">{railStack.length>0&&<button className="back-link" onClick={goUp}>‹ Up one level</button>}{railContext&&<button className="back-link secondary" onClick={loadAllParts}>All Parts</button>}</div></div>
      {error&&<div className="error">{error}</div>}
      <div className="result-stack">{results.map(r=><button key={r.id} className={selected?.id===r.id?'hit active':'hit'} onClick={()=>choose(r)}><span>{label(r.node_type)}</span><strong>{r.title}</strong><small>{truncate(r.snippet||r.text,128)}</small></button>)}</div>
    </aside>

    <main className="canvas">
      <div className="canvas-meta"><strong>{selected?.title||'Select a node'}</strong><span>{activeRep.label} · {visibleGraph.nodes.length} shown · {visibleGraph.edges.length} visible links · {Object.values(graph.available_edge_types||{}).reduce((a,b)=>a+b,0)} direct links available</span></div>
      <Graph graph={visibleGraph} selected={selected} detail={detail} nodeTypes={nodeTypes} onToggleNodeType={toggleNodeType} onSelect={n=>{setDetail(n);setPanelOpen(true);}} onOpen={choose}/>
    </main>

    <aside className={panelOpen?'inspector open':'inspector'}>
      <div className="tabs"><button className={sideTab==='explore'?'on':''} onClick={()=>setSideTab('explore')}>Explore</button><button className={sideTab==='discover'?'on':''} onClick={()=>setSideTab('discover')}>Discover</button><button className={sideTab==='analysis'?'on':''} onClick={()=>setSideTab('analysis')}>Analysis</button></div>
      {sideTab==='explore'&&<Explore node={detail} edges={selectedEdges} graph={graph} onChoose={choose}/>} 
      {sideTab==='discover'&&<Discover interesting={analysis.interesting} onChoose={choose}/>} 
      {sideTab==='analysis'&&<Analysis analysis={analysis} onChoose={choose}/>} 
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
    <svg ref={svgRef} className={drag?'graph panning':'graph'} viewBox="0 0 1200 820"
      onPointerDown={startPan} onPointerMove={movePan} onPointerUp={endPan} onPointerLeave={()=>{endPan();setHover(null)}}
      onWheel={e=>{e.preventDefault(); const dz=e.deltaY>0?.92:1.08; setView(v=>({...v,z:Math.max(.55,Math.min(1.8,v.z*dz))}));}}>
      <g transform={`translate(${view.x} ${view.y}) scale(${view.z})`}>
        {edges.map(e=>{const a=byId.get(e.from_node_id),b=byId.get(e.to_node_id); if(!a||!b)return null; const inf=!EXPLICIT.has(e.source_method); return <line key={e.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={inf?'edge inferred':'edge'} strokeWidth={Math.max(1,(e.confidence||.45)*2.4)} />})}
        {nodes.map(n=><g key={n.id} className={`node ${selected?.id===n.id?'selected':''} ${detail?.id===n.id?'focus':''}`}
          onClick={()=>onSelect(n)} onDoubleClick={()=>onOpen(n)}
          onPointerEnter={e=>setHover({node:n,x:e.clientX,y:e.clientY})}
          onPointerMove={e=>setHover({node:n,x:e.clientX,y:e.clientY})}
          onPointerLeave={()=>setHover(null)}>
          <circle cx={n.x} cy={n.y} r={r(n)} fill={COLOUR[n.node_type]||'#64748b'} />
          <text x={n.x} y={n.y+r(n)+15} textAnchor="middle">{truncate(n.title||n.id,n.id===selected?.id?38:24)}</text>
        </g>)}
      </g>
    </svg>
    {hover&&<div className="node-tip" style={{left:hover.x+14,top:hover.y+14}}><span>{label(hover.node.node_type)}</span><strong>{hover.node.title}</strong><small>{truncate(hover.node.text||hover.node.url||'',180)}</small></div>}
    <Legend active={nodeTypes} onToggle={onToggleNodeType} />
    <div className="zoom"><button onClick={()=>setView(v=>({...v,z:Math.min(1.8,v.z*1.15)}))}>＋</button><button onClick={()=>setView(v=>({...v,z:Math.max(.55,v.z*.85)}))}>−</button><button onClick={()=>setView({x:0,y:0,z:1})}>⌂</button></div>
  </div>;
}

function Legend({active,onToggle}){
  const items=['rule','chapter','part','defined_term','guidance_document','guidance_section','guidance_paragraph','topic','obligation_pattern','legal_instrument','external_reference'];
  return <div className="legend">{items.map(t=><button key={t} className={active?.has(t)?'on':'off'} onClick={()=>onToggle(t)} title={`Toggle ${label(t)}`}><i style={{background:COLOUR[t]||'#64748b'}} /> <span>{label(t)}</span></button>)}<div><i className="line"/> <span>explicit</span></div><div><i className="line dash"/> <span>inferred</span></div></div>
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
      <span className="content-body"><span className="content-meta"><b>{label(node.node_type)}</b>{number&&<em>{number}</em>}{kids.length>0&&<em>{kids.length} item{kids.length===1?'':'s'}</em>}</span><strong>{node.title}</strong>{node.text&&<small>{truncate(node.text,190)}</small>}</span>
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
      <p className="text">{node.text?truncate(node.text,1300):'No body text for this node.'}</p>
    </Collapsible>
    {groups.length
      ? groups.map(([edgeType,items],i)=><Collapsible key={edgeType} title={label(edgeType)} count={`${items.length} link${items.length===1?'':'s'}`} open={i<2}>
          <div className="edge-list">{items.slice(0,40).map(e=>{const other=byId.get(e.from_node_id===node.id?e.to_node_id:e.from_node_id);return <button key={e.id} onClick={()=>other&&onChoose(other)}><span>{e.source_method} · {Math.round((e.confidence||0)*100)}%</span><strong>{other?.title||'Unloaded node'}</strong>{e.evidence_text&&<small>{truncate(e.evidence_text,160)}</small>}</button>})}</div>
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
function Discover({interesting,onChoose}){return <div className="pane list">{interesting.map(e=><button key={e.id} onClick={()=>onChoose({id:e.from_node_id,title:e.from_title,node_type:e.from_type})}><span>{label(e.edge_type)} · {Math.round((e.confidence||0)*100)}%</span><strong>{e.from_title}</strong><small>→ {e.to_title}</small><em>{e.why}</em></button>)}</div>}
function Analysis({analysis,onChoose}){return <div className="pane list"><div className="mini-metrics"><div><b>{analysis.stats?.nodes?.toLocaleString()||'…'}</b><span>nodes</span></div><div><b>{analysis.stats?.edges?.toLocaleString()||'…'}</b><span>edges</span></div><div><b>{analysis.stats?.edge_methods?.regex_named_reference||'…'}</b><span>named refs</span></div></div><h3>Central</h3>{analysis.centrality.map((x,i)=><button key={x.node?.id||i} onClick={()=>x.node&&onChoose(x.node)}><span>{x.degree} links</span><strong>{x.node?.title}</strong></button>)}<h3>Bridges</h3>{analysis.bridges.map((x,i)=><button key={x.node?.id||i} onClick={()=>x.node&&onChoose(x.node)}><span>{Number(x.betweenness||0).toFixed(4)}</span><strong>{x.node?.title}</strong></button>)}</div>}

function filterGraph(graph,nodeTypes,selectedId,showInsurance=true){
  const keepNodes=(graph.nodes||[]).filter(n=>(nodeTypes.has(n.node_type)||n.id===selectedId) && (showInsurance || n.id===selectedId || !isInsuranceNode(n)));
  const keepIds=new Set(keepNodes.map(n=>n.id));
  return {...graph,nodes:keepNodes,edges:(graph.edges||[]).filter(e=>keepIds.has(e.from_node_id)&&keepIds.has(e.to_node_id))};
}

function isInsuranceNode(n){
  const hay=[n.title,n.text,n.url,n.metadata?.part_title,n.metadata?.document_title,n.metadata?.topic].filter(Boolean).join(' ').toLowerCase();
  return /insurance|insurer|solvency ii|sii|non-solvency|policyholder|with-profits|actuar|matching adjustment|technical provisions|own funds/.test(hay);
}

function layout(graph, centreId){
  const nodes=[...(graph.nodes||[])], edges=graph.edges||[]; if(!nodes.length)return{nodes,edges};
  const degree=new Map(nodes.map(n=>[n.id,0])); edges.forEach(e=>{degree.set(e.from_node_id,(degree.get(e.from_node_id)||0)+1); degree.set(e.to_node_id,(degree.get(e.to_node_id)||0)+1)});
  const centre=nodes.find(n=>n.id===centreId)||nodes[0], others=nodes.filter(n=>n.id!==centre.id).sort((a,b)=>(degree.get(b.id)||0)-(degree.get(a.id)||0));
  centre.x=600; centre.y=410; centre.degree=degree.get(centre.id)||1;
  others.forEach((n,i)=>{const ring=i<20?1:i<64?2:3; const idx=ring===1?i:ring===2?i-20:i-64; const count=ring===1?Math.min(20,others.length):ring===2?Math.min(44,Math.max(1,others.length-20)):Math.max(1,others.length-64); const a=(Math.PI*2*idx/count)+(ring*.21); const rad=ring===1?205:ring===2?335:455; n.x=600+Math.cos(a)*rad; n.y=410+Math.sin(a)*rad*.76; n.degree=degree.get(n.id)||1;});
  return{nodes:[centre,...others],edges};
}
function r(n){return Math.min(25,(n.node_type==='part'?14:n.node_type==='topic'?13:n.node_type==='defined_term'?11:9)+Math.sqrt(n.degree||1));}
function label(v){
  if(['chapter','rule','guidance_section','guidance_paragraph'].includes(v)) return 'provision';
  if(v==='defined_term') return 'defined term';
  if(v==='external_reference') return 'external reference';
  if(v==='legal_instrument') return 'legal instrument';
  if(v==='obligation_pattern') return 'obligation pattern';
  return String(v||'').replaceAll('_',' ');
}
function truncate(s='',n=120){return s&&s.length>n?s.slice(0,n-1)+'…':s}

createRoot(document.getElementById('root')).render(<App/>);
