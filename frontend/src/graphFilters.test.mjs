import assert from 'node:assert/strict';
import { test } from 'node:test';
import { isInsuranceNode } from './graphFilters.js';

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
