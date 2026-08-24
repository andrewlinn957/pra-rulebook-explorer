import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

function lastRuleBody(sourceText, selector) {
  const matches = [...sourceText.matchAll(new RegExp(`(?:^|\\n)\\s*${selector}\\s*\\{([^}]*)\\}`, 'g'))];
  return matches.at(-1)?.[1] || '';
}

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

test('graph rail is wider while result rows stay dense and fixed', () => {
  assert.match(lastRuleBody(styles, '\\.shell'), /grid-template-columns:224px minmax\(0,1fr\) 370px/);
  assert.match(lastRuleBody(styles, '\\.result-stack'), /grid-auto-rows:30px/);
  assert.match(lastRuleBody(styles, '\\.hit'), /height:30px[\s\S]*?padding:5px 8px/);
  assert.match(lastRuleBody(styles, '\\.hit strong'), /font-size:10px[\s\S]*?line-height:1\.15/);
});

test('graph rail result rows do not reserve space for decorative bullets', () => {
  const hitRule = lastRuleBody(styles, '\\.hit');
  assert.match(hitRule, /padding:5px 8px/);
  assert.doesNotMatch(styles, /\.hit:before/);
  assert.doesNotMatch(styles, /\.hit\.active:before/);
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
