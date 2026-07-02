import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { classifyUnresolvedReferenceRow, buildUnresolvedActionQueues } from './unresolvedWorkflow.js';

describe('unresolved links workflow classifier', () => {
  it('tells the reviewer to resolve PRA rule-looking links internally', () => {
    const row = classifyUnresolvedReferenceRow({
      target_type: 'rule_reference',
      target_title: 'Liquidity CRR 4.1',
      target_url: 'https://www.prarulebook.co.uk/pra-rules/liquidity-crr/4',
      source_title: 'Liquidity CRR',
    });

    assert.equal(row.next_action, 'Resolve to Rulebook node');
    assert.match(row.why, /internal PRA/i);
    assert.equal(row.action_group, 'resolve-internal');
  });

  it('tells the reviewer to check or repair raw external URLs', () => {
    const row = classifyUnresolvedReferenceRow({
      target_type: 'external_reference',
      target_title: 'https://example.com/old-page',
      target_url: 'https://example.com/old-page',
    });

    assert.equal(row.next_action, 'Check URL');
    assert.match(row.why, /raw URL/i);
    assert.equal(row.action_group, 'check-url');
  });

  it('tells the reviewer to classify generic link text rather than match blindly', () => {
    const row = classifyUnresolvedReferenceRow({
      target_type: 'external_reference',
      target_title: 'here',
      source_text: 'See here for the relevant statement of policy.',
    });

    assert.equal(row.next_action, 'Inspect source context');
    assert.match(row.why, /generic/i);
    assert.equal(row.action_group, 'inspect-context');
  });

  it('builds labelled queues with counts and filtered rows', () => {
    const rows = [
      { target_type: 'rule_reference', target_title: 'Depositor Protection 1.2', target_url: '/pra-rules/depositor-protection/1' },
      { target_type: 'external_reference', target_title: 'https://example.com/a', target_url: 'https://example.com/a' },
      { target_type: 'external_reference', target_title: 'click here' },
    ];

    const queues = buildUnresolvedActionQueues(rows);
    assert.deepEqual(queues.map(q => [q.id, q.count]), [
      ['resolve-internal', 1],
      ['check-url', 1],
      ['inspect-context', 1],
    ]);
    assert.equal(queues[0].rows[0].next_action, 'Resolve to Rulebook node');
  });
});
