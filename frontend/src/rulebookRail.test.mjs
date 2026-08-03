import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

test('rulebook rail result rows are name-only', () => {
  const railRender = source.match(/<div className="result-stack">([\s\S]*?)<\/div>\s*<\/aside>/)?.[1] || '';

  assert.match(railRender, /<strong><NodeTitle node=\{r\}\/><\/strong>/);
  assert.doesNotMatch(railRender, /label\(r\.node_type\)/);
  assert.doesNotMatch(railRender, /truncate\(r\.snippet\|\|r\.text,128\)/);
});

test('rulebook rail result rows use compact spacing', () => {
  assert.match(styles, /\.result-stack\{padding:6px;display:grid;gap:2px\}/);
  assert.match(styles, /\.hit\{[^}]*padding:6px 8px/);
});

test('rulebook rail exposes official part audience filters only on all parts', () => {
  assert.match(source, /const PART_AUDIENCE_FILTERS=\[/);
  assert.match(source, /\{key:'crr',label:'CRR firms',category:'CRR Firms'\}/);
  assert.match(source, /\{key:'no-authorised',label:'No authorised persons',category:'Non-authorised persons'\}/);
  assert.match(source, /const showPartAudienceFilters=!railContext&&results\.some\(r=>r\.node_type==='part'&&partAudienceCategories\(r\)\.length\)/);
  assert.match(source, /results\.filter\(r=>partAudienceFilter==='all'\|\|partAudienceCategories\(r\)\.includes\(partAudienceFilter\)\)/);
});

test('empty heading message does not imply missing child provisions exist', () => {
  assert.match(source, /const hasOutgoingChild=edges\.some\(edge=>edge\.edge_type==='contains'&&edge\.from_node_id===node\?\.id\)/);
  assert.match(source, /No child provision text is currently linked for this heading/);
});
