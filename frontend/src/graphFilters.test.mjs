import assert from 'node:assert/strict';
import { test } from 'node:test';
import { filterGraph, isInsuranceNode } from './graphFilters.js';

test('insurance filter does not hide Fees provisions that mention insurance in their text', () => {
  const node = {
    node_type: 'rule',
    title: '3.4',
    text: 'for firms in the general insurance fee block (A3)',
    metadata: { part_title: 'Fees' },
  };

  assert.equal(isInsuranceNode(node), false);
});

test('insurance filter hides material that belongs to an insurance part', () => {
  const node = {
    node_type: 'rule',
    title: '2.1',
    text: 'plain rule text',
    metadata: { part_title: 'Insurance Special Purpose Vehicles' },
  };

  assert.equal(isInsuranceNode(node), true);
});

test('graph filtering drops orphan nodes created by hidden edges', () => {
  const graph = {
    nodes: [
      { id: 'return:PRA110', node_type: 'DataItem' },
      { id: 'template:PRA110', node_type: 'Template' },
      { id: 'datapoints:PRA110', node_type: 'DataPointGroup' },
    ],
    edges: [
      { id: 'e1', from_node_id: 'return:PRA110', to_node_id: 'template:PRA110', edge_type: 'USES_TEMPLATE' },
      { id: 'e2', from_node_id: 'template:PRA110', to_node_id: 'datapoints:PRA110', edge_type: 'SUMMARISES_DATAPOINTS' },
    ],
  };

  const filtered = filterGraph(
    graph,
    new Set(['DataItem', 'Template', 'DataPointGroup']),
    new Set(['USES_TEMPLATE']),
    'all',
    null,
    true,
  );

  assert.deepEqual(filtered.nodes.map(n => n.id), ['return:PRA110', 'template:PRA110']);
  assert.deepEqual(filtered.edges.map(e => e.id), ['e1']);
});

test('graph filtering keeps selected orphan so the inspector focus is not lost', () => {
  const graph = {
    level: 'reporting_overview',
    nodes: [
      { id: 'return:PRA110', node_type: 'DataItem' },
      { id: 'datapoints:PRA110', node_type: 'DataPointGroup' },
    ],
    edges: [],
  };

  const filtered = filterGraph(
    graph,
    new Set(['DataItem', 'DataPointGroup']),
    new Set(['USES_TEMPLATE']),
    'all',
    'datapoints:PRA110',
    true,
  );

  assert.deepEqual(filtered.nodes.map(n => n.id), ['return:PRA110', 'datapoints:PRA110']);
});
