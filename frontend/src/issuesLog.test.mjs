import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { filterIssues, issueCounts, issueStatusLabel } from './issuesLog.js';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

const issuesViewSource = source.slice(source.indexOf('function IssuesLogView'), source.indexOf('function IssueReportModal'));
const topbarSource = source.slice(source.indexOf('<header className="topbar">'), source.indexOf('</header>') + '</header>'.length);
const settingsSource = source.slice(source.indexOf('<nav className="rail-utilities"'), source.indexOf('</nav>') + '</nav>'.length);

test('issue helpers count and filter reports without changing their order', () => {
  const items = [
    { id: 'one', status: 'open' },
    { id: 'two', status: 'resolved' },
    { id: 'three', status: 'open' },
  ];

  assert.deepEqual(filterIssues(items, 'open').map(item => item.id), ['one', 'three']);
  assert.deepEqual(filterIssues(items, 'all'), items);
  assert.deepEqual(issueCounts(items), { all: 3, open: 2, in_progress: 0, resolved: 1, wont_fix: 0 });
  assert.equal(issueStatusLabel('in_progress'), 'In progress');
});

test('issues log is a settings-only compact table with maintenance actions', () => {
  assert.match(source, /view==='issues'/u);
  assert.match(settingsSource, /Issues log/u);
  assert.doesNotMatch(topbarSource, /Issues log/u);
  assert.match(issuesViewSource, /<table[^>]*className="issues-table"/u);
  assert.match(issuesViewSource, /Edit/u);
  assert.match(issuesViewSource, /Delete/u);
  assert.doesNotMatch(issuesViewSource, /Add issue/u);
  assert.doesNotMatch(issuesViewSource, /node selector|node search|change node/iu);
  assert.match(styles, /\.issues-table\{/u);
});

test('issue maintenance updates only description and status and deletes with confirmation', () => {
  assert.match(issuesViewSource, /method:'PATCH'/u);
  assert.match(issuesViewSource, /method:'DELETE'/u);
  assert.match(issuesViewSource, /window\.confirm/u);
  assert.match(issuesViewSource, /description/u);
  assert.match(issuesViewSource, /status/u);
  assert.doesNotMatch(issuesViewSource, /node\.id|node_id|setIssueNode/iu);
});

test('existing graph and reader report creation remains wired to the report endpoint', () => {
  assert.match(source, /API_BASE\+'\/issues\/node'/u);
  assert.match(source, /Report an issue with this node/u);
});
