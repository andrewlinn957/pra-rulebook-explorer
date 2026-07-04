import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');

function literalSetItems(constName) {
  const match = source.match(new RegExp(`const ${constName} = new Set\\(\\[([^\\]]*)\\]\\);`));
  assert.ok(match, `${constName} declaration should be a literal Set`);
  return [...match[1].matchAll(/'([^']+)'/g)].map(([, value]) => value);
}

function representationTypes(key) {
  const match = source.match(new RegExp(`${key}: \\{[^}]*types:\\[([^\\]]*)\\]`));
  assert.ok(match, `${key} representation should declare types`);
  if (match[1].includes('...DEFAULT_TYPES')) return literalSetItems('DEFAULT_TYPES');
  return [...match[1].matchAll(/'([^']+)'/g)].map(([, value]) => value);
}

test('default graph shows only hierarchy and cross-reference edges', () => {
  assert.deepEqual(literalSetItems('DEFAULT_TYPES'), ['contains', 'references']);
});

test('combined representation uses the default edge set without topic matching', () => {
  assert.deepEqual(representationTypes('combined'), ['contains', 'references']);
});

test('topic matching is not exposed in the graph UI', () => {
  assert.doesNotMatch(source, /'has_topic'/);
  assert.doesNotMatch(source, /'topic'/);
  assert.doesNotMatch(source, /'topic_cluster'/);
  assert.doesNotMatch(source, /keyword_topic/);
});

test('definitions representation still lets users opt into shared defined term edges', () => {
  assert.ok(representationTypes('definitions').includes('shares_defined_term'));
});

test('edge tooltips remain available while only parallel-link count badges render inline', () => {
  assert.match(source, /onLinkHover=\{edge=>setHoverEdge\(edge\|\|null\)\}/);
  assert.match(source, /edgeTooltip\(hoverEdge,selected\?\.id\)/);
  assert.match(source, /function drawGraphLink\(edge,ctx,globalScale,selected\)/);
  assert.match(source, /if\(edge\.parallelCount>1\) drawParallelEdgeCount/);
  assert.doesNotMatch(source, /drawCanvasLabel\(ctx,label,\(sx\+tx\)\/2,\(sy\+ty\)\/2/);
});

test('non-hierarchy graph edges show directional arrows', () => {
  assert.match(source, /linkDirectionalArrowLength=\{e=>e\.edge_type==='contains'\?0:10\.5\}/);
  assert.match(source, /linkDirectionalArrowRelPos=\{e=>e\.edge_type==='contains'\?1:\.72\}/);
  assert.match(source, /linkDirectionalArrowColor=\{e=>edgeDirectionColour\(e,selected\?\.id\)\}/);
  assert.match(source, /edgeDirectionGlyph\(e,node\.id\)/);
});

test('external document links render readable PDF/XLS chips outside the canvas', () => {
  assert.match(source, /function NodeTitle\(\{node\}\)/);
  assert.match(source, /className=\{`doc-chip \$\{badge\.kind\}`\}/);
  assert.match(source, /<NodeTitle node=\{other\}\/>/);
});
