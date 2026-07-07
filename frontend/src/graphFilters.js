const INSURANCE_PART_RE = /insurance|insurer|solvency ii|sii|non-solvency|policyholder|with-profits|actuar|matching adjustment|technical provisions|own funds/i;

export function isInsuranceNode(node) {
  const partContext = [
    node?.metadata?.part_title,
    node?.metadata?.document_title,
    ['part', 'chapter', 'guidance_document', 'guidance_section'].includes(node?.node_type) ? node?.title : '',
  ].filter(Boolean).join(' ');
  return INSURANCE_PART_RE.test(partContext);
}

export function filterGraph(graph,nodeTypes,relationshipTypes,originFilter='all',selectedId=null,showInsurance=true){
  const keepNodes=(graph.nodes||[]).filter(n=>(nodeTypes.has(n.node_type)||n.id===selectedId) && (showInsurance || n.id===selectedId || !isInsuranceNode(n)));
  const keepIds=new Set(keepNodes.map(n=>n.id));
  const edges=(graph.edges||[]).filter(e=>keepIds.has(e.from_node_id)&&keepIds.has(e.to_node_id)&&(!relationshipTypes?.size||relationshipTypes.has(e.edge_type))&&originMatches(e,originFilter));
  const linkedIds=new Set();
  for(const edge of edges){ linkedIds.add(edge.from_node_id); linkedIds.add(edge.to_node_id); }
  const nodes=keepNodes.filter(n=>linkedIds.has(n.id)||n.id===selectedId||isRootNode(n,graph));
  return {...graph,nodes,edges};
}

function isRootNode(node,graph){
  if(node?.id===graph?.centre_id) return true;
  if(graph?.level==='reporting_overview' && node?.node_type==='DataItem') return true;
  return false;
}

function originMatches(edge,originFilter){
  if(originFilter==='all') return true;
  const inferred=edge.source_method==='inferred' || edge.source_method==='lexical_similarity' || edge.source_method==='semantic_similarity' || edge.metadata?.inferred;
  return originFilter==='inferred' ? inferred : !inferred;
}
