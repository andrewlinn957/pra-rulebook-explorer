export const REPORTING_CHILD_EDGE_TYPES = new Set([
  'HAS_REGIME',
  'HAS_COLLECTION',
  'HAS_EDITION',
  'HAS_TEMPLATE_RESOURCE',
  'HAS_INSTRUCTION_RESOURCE',
  'HAS_RESOURCE',
  'CONTAINS_SHEET',
  'IMPLEMENTS_TEMPLATE',
  'CONTAINS_INSTRUCTION_SECTION',
  'HAS_TAXONOMY_RESOURCE',
  'HAS_ENTRY_POINT',
  'ENCODES_REQUIREMENT',
  'USES_TEMPLATE',
  'USES_INSTRUCTIONS',
  'EVIDENCED_BY',
  'HAS_SCOPE_RULE',
  'SUMMARISES_DATAPOINTS',
  'HAS_DATAPOINT',
  'REPORTS_CONCEPT',
]);

export const REPORTING_EDGE_GROUPS = [
  {
    key: 'structure',
    label: 'Structure',
    edgeTypes: ['HAS_REGIME', 'HAS_COLLECTION', 'BELONGS_TO_REGIME', 'BELONGS_TO_COLLECTION', 'HAS_EDITION', 'SUPERSEDES'],
  },
  {
    key: 'documents',
    label: 'Forms & guidance',
    edgeTypes: ['HAS_TEMPLATE_RESOURCE', 'HAS_INSTRUCTION_RESOURCE', 'HAS_RESOURCE', 'CONTAINS_SHEET', 'IMPLEMENTS_TEMPLATE', 'CONTAINS_INSTRUCTION_SECTION', 'USES_TEMPLATE', 'USES_INSTRUCTIONS', 'EVIDENCED_BY'],
  },
  {
    key: 'rules',
    label: 'Related rules',
    edgeTypes: ['LEGAL_BASIS', 'APPLIES_TO', 'HAS_SCOPE_RULE', 'MAY_BE_AFFECTED_BY_PERMISSION', 'REFERENCES_RULE', 'REFERENCES_SOURCE', 'REFERENCES_EXTERNAL', 'REFERENCES_RETURN', 'REFERENCES_TEMPLATE'],
  },
  {
    key: 'technical',
    label: 'Technical detail',
    edgeTypes: ['SUPPORTED_BY_TAXONOMY', 'HAS_TAXONOMY_RESOURCE', 'HAS_ENTRY_POINT', 'ENCODES_REQUIREMENT', 'SUMMARISES_DATAPOINTS', 'HAS_DATAPOINT', 'REPORTS_CONCEPT'],
  },
];

export const REPORTING_OVERVIEW_EDGE_GROUP_KEYS = ['structure', 'documents', 'rules'];
export const REPORTING_REQUIREMENT_EDGE_GROUP_KEYS = ['documents', 'rules'];

const REPORTING_EDGE_GROUP_BY_TYPE = new Map(
  REPORTING_EDGE_GROUPS.flatMap(group => group.edgeTypes.map(edgeType => [edgeType, group])),
);

export function reportingEdgeGroup(edgeType) {
  return REPORTING_EDGE_GROUP_BY_TYPE.get(edgeType) || null;
}

export function reportingEdgeTypesForGroups(groupKeys) {
  return new Set(
    REPORTING_EDGE_GROUPS
      .filter(group => groupKeys.has(group.key))
      .flatMap(group => group.edgeTypes),
  );
}

export function reportingEdgeGroupCounts(graph) {
  const counts = Object.fromEntries(REPORTING_EDGE_GROUPS.map(group => [group.key, 0]));
  for (const edge of graph?.edges || []) {
    const group = reportingEdgeGroup(edge.edge_type);
    if (group) counts[group.key] += 1;
  }
  return counts;
}

export function reportingRequirementEditions(selectedRow, returns) {
  if (!selectedRow?.requirement_id) return [];
  return (returns || [])
    .filter(row => row.requirement_id === selectedRow.requirement_id)
    .sort((left, right) => {
      const dateOrder = String(left.effective_from || '').localeCompare(String(right.effective_from || ''));
      return dateOrder || String(left.return_id || '').localeCompare(String(right.return_id || ''));
    });
}

export function reportingEditionOptionLabel(row) {
  const status = {
    current: 'Current',
    future: 'Future',
    superseded: 'Superseded',
  }[String(row?.status || '').toLowerCase()] || 'Edition';
  return row?.effective_text ? `${status} · ${row.effective_text}` : status;
}

export function reportingChildGroups(node, graph) {
  if (!node) return [];
  const nodesById = new Map((graph?.nodes || []).map(item => [item.id, item]));
  const groups = new Map();
  for (const edge of graph?.edges || []) {
    if (edge.from_node_id !== node.id || !REPORTING_CHILD_EDGE_TYPES.has(edge.edge_type)) continue;
    const child = nodesById.get(edge.to_node_id);
    if (!child) continue;
    const group = reportingEdgeGroup(edge.edge_type) || { key: 'other', label: 'Other' };
    if (!groups.has(group.key)) groups.set(group.key, { ...group, children: new Map() });
    groups.get(group.key).children.set(child.id, child);
  }
  return [...groups.values()].map(group => ({
    edgeType: group.key,
    label: group.label,
    children: [...group.children.values()],
  }));
}

export function reportingParentNodes(node, graph) {
  if (!node) return [];
  const nodesById = new Map((graph?.nodes || []).map(item => [item.id, item]));
  const parents = new Map();
  for (const edge of graph?.edges || []) {
    if (edge.to_node_id !== node.id || !REPORTING_CHILD_EDGE_TYPES.has(edge.edge_type)) continue;
    const parent = nodesById.get(edge.from_node_id);
    if (parent) parents.set(parent.id, parent);
  }
  return [...parents.values()];
}

const REPORTING_RESOURCE_EDGE_TYPES = new Set([
  'HAS_TEMPLATE_RESOURCE',
  'HAS_INSTRUCTION_RESOURCE',
  'HAS_RESOURCE',
]);

export function reportingSourceNodes(node, graph) {
  if (!node) return [];
  const nodesById = new Map((graph?.nodes || []).map(item => [item.id, item]));
  const edges = graph?.edges || [];
  const sourceIds = new Set();
  const editionIds = new Set(node.node_type === 'RequirementEdition' ? [node.id] : []);

  for (const edge of edges) {
    if (edge.from_node_id === node.id && edge.edge_type === 'HAS_EDITION') editionIds.add(edge.to_node_id);
    if (edge.from_node_id === node.id && REPORTING_RESOURCE_EDGE_TYPES.has(edge.edge_type)) sourceIds.add(edge.to_node_id);
    if (edge.from_node_id === node.id && edge.edge_type === 'EVIDENCED_BY') sourceIds.add(edge.to_node_id);
  }
  for (const edge of edges) {
    if (editionIds.has(edge.from_node_id) && REPORTING_RESOURCE_EDGE_TYPES.has(edge.edge_type)) sourceIds.add(edge.to_node_id);
    if (editionIds.has(edge.from_node_id) && edge.edge_type === 'EVIDENCED_BY') sourceIds.add(edge.to_node_id);
  }
  return [...sourceIds].map(id => nodesById.get(id)).filter(Boolean);
}

export function reportingOneHopGraph(graph, centreId) {
  if (!centreId || !(graph?.nodes || []).some(node => node.id === centreId)) return graph;
  const edges = (graph.edges || []).filter(
    edge => edge.from_node_id === centreId || edge.to_node_id === centreId,
  );
  const nodeIds = new Set([centreId]);
  for (const edge of edges) {
    nodeIds.add(edge.from_node_id);
    nodeIds.add(edge.to_node_id);
  }
  return {
    ...graph,
    centre_id: centreId,
    nodes: (graph.nodes || []).filter(node => nodeIds.has(node.id)),
    edges,
  };
}
