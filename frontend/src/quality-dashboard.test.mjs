import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

test('quality tab is a plain-English issue review rather than an audit cockpit', () => {
  assert.match(source, /className="quality quality-redesign"/);
  assert.match(source, /What needs attention/);
  assert.match(source, /Can I trust the explorer\?/);
  assert.match(source, /What this means/);
  assert.match(source, /Why it matters/);
  assert.match(source, /What to do next/);
  assert.match(source, /Show evidence/);
  assert.doesNotMatch(source, /Risk<\/span>/);
  assert.doesNotMatch(source, /audit-cockpit/);
  assert.doesNotMatch(source, /Priority<\/span>/);
});

test('quality redesign styles make issue cards and evidence drawers first-class', () => {
  assert.match(styles, /\.quality-redesign/);
  assert.match(styles, /\.quality-summary-grid/);
  assert.match(styles, /\.quality-issue-card/);
  assert.match(styles, /\.quality-evidence-drawer/);
});
