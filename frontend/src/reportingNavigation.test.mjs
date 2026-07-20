import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  REPORTING_EDGE_GROUPS,
  reportingChildGroups,
  reportingEdgeGroupCounts,
  reportingEdgeTypesForGroups,
  reportingOneHopGraph,
  reportingParentNodes,
  reportingSourceNodes,
} from './reportingNavigation.js';

const graph = {
  nodes: [
    { id: 'return', title: 'PRA110' },
    { id: 'instructions', title: 'PRA110 instructions' },
    { id: 'template', title: 'PRA110 template' },
    { id: 'reference', title: 'External reference' },
  ],
  edges: [
    { from_node_id: 'return', to_node_id: 'instructions', edge_type: 'USES_INSTRUCTIONS' },
    { from_node_id: 'return', to_node_id: 'template', edge_type: 'USES_TEMPLATE' },
    { from_node_id: 'return', to_node_id: 'reference', edge_type: 'REFERENCES_EXTERNAL' },
  ],
};

describe('reporting child navigation', () => {
  it('groups child nodes into human-facing categories', () => {
    assert.deepEqual(
      reportingChildGroups(graph.nodes[0], graph).map(group => [group.edgeType, group.label, group.children.map(node => node.id)]),
      [['documents', 'Forms & guidance', ['instructions', 'template']]],
    );
  });

  it('does not present cross-references as child nodes', () => {
    const ids = reportingChildGroups(graph.nodes[0], graph).flatMap(group => group.children.map(node => node.id));
    assert.equal(ids.includes('reference'), false);
  });

  it('finds structural parents for up-navigation', () => {
    assert.deepEqual(reportingParentNodes(graph.nodes[1], graph).map(node => node.id), ['return']);
  });

  it('inherits edition resources when inspecting a stable requirement', () => {
    const ontology = {
      nodes: [
        { id: 'requirement', node_type: 'ReportingRequirement' },
        { id: 'edition', node_type: 'RequirementEdition' },
        { id: 'template-resource', node_type: 'ReportingResource', url: 'https://example.test/annex-i.xlsx' },
        { id: 'instruction-resource', node_type: 'ReportingResource', url: 'https://example.test/annex-ii.pdf' },
      ],
      edges: [
        { from_node_id: 'requirement', to_node_id: 'edition', edge_type: 'HAS_EDITION' },
        { from_node_id: 'edition', to_node_id: 'template-resource', edge_type: 'HAS_TEMPLATE_RESOURCE' },
        { from_node_id: 'edition', to_node_id: 'instruction-resource', edge_type: 'HAS_INSTRUCTION_RESOURCE' },
      ],
    };
    assert.deepEqual(
      reportingSourceNodes(ontology.nodes[0], ontology).map(node => node.id),
      ['template-resource', 'instruction-resource'],
    );
  });

  it('offers four relationship categories and hides technical detail by default', () => {
    assert.deepEqual(REPORTING_EDGE_GROUPS.map(group => group.label), [
      'Structure', 'Forms & guidance', 'Related rules', 'Technical detail',
    ]);
    const visible = reportingEdgeTypesForGroups(new Set(['structure', 'documents', 'rules']));
    assert.equal(visible.has('HAS_EDITION'), true);
    assert.equal(visible.has('HAS_TEMPLATE_RESOURCE'), true);
    assert.equal(visible.has('REFERENCES_RULE'), true);
    assert.equal(visible.has('SUPPORTED_BY_TAXONOMY'), false);
  });

  it('counts underlying edge types under their combined categories', () => {
    assert.deepEqual(reportingEdgeGroupCounts(graph), {
      structure: 0,
      documents: 2,
      rules: 1,
      technical: 0,
    });
  });

  it('limits the canvas to the selected node and its immediate neighbours', () => {
    const deepGraph = {
      nodes: [{ id: 'root' }, { id: 'child' }, { id: 'grandchild' }, { id: 'unrelated' }],
      edges: [
        { from_node_id: 'root', to_node_id: 'child', edge_type: 'HAS_EDITION' },
        { from_node_id: 'child', to_node_id: 'grandchild', edge_type: 'HAS_RESOURCE' },
      ],
    };
    const oneHop = reportingOneHopGraph(deepGraph, 'root');
    assert.deepEqual(oneHop.nodes.map(node => node.id), ['root', 'child']);
    assert.equal(oneHop.edges.length, 1);
  });
});
