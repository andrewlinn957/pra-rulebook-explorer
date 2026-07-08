import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { buildQualityQueues, filterQueueRows, summariseQueue } from './qualityWorkbench.js';

describe('quality workbench queue model', () => {
  it('exposes only the two primary queues in the compact selector', () => {
    const queues = buildQualityQueues({
      feedback: { items: [{ id: 'f1', status: 'pending' }] },
      validation: { checks: [{ check: 'unresolved_references', status: 'warn', samples: [{ target_title: 'old link' }] }] },
    });

    assert.deepEqual(queues.map(q => q.id), ['feedback', 'unverified-links']);
    assert.equal(queues[0].label, 'Feedback');
    assert.equal(queues[1].label, 'Unverified links');
  });

  it('summarises node feedback by work state without adding extra dashboard cards', () => {
    const summary = summariseQueue({
      id: 'feedback',
      items: [
        { status: 'pending' },
        { status: 'failed' },
        { status: 'completed' },
      ],
    });

    assert.equal(summary.primary, '2 open');
    assert.equal(summary.secondary, '1 done');
  });

  it('uses unresolved reference samples for the unverified links queue', () => {
    const queues = buildQualityQueues({
      validation: { checks: [{ check: 'unresolved_references', status: 'warn', samples: [{ target_title: 'A' }, { target_title: 'B' }] }] },
    });

    assert.equal(queues[1].count, 2);
    assert.deepEqual(queues[1].rows.map(r => r.target_title), ['A', 'B']);
  });

  it('filters queue rows using visible row values', () => {
    const rows = filterQueueRows([
      { source_title: 'Liquidity Coverage Ratio', target_title: 'Article 8' },
      { source_title: 'Capital Buffers', target_title: 'SS6/14' },
    ], 'liquidity');

    assert.deepEqual(rows, [{ source_title: 'Liquidity Coverage Ratio', target_title: 'Article 8' }]);
  });
});
