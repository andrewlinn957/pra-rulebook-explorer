import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { CHART_SEQUENCE, COLOURS, EDGE_COLOURS, MATERIAL_COLOURS } from './colourTokens.js';

test('colour system exposes the approved core palette', () => {
  assert.equal(COLOURS.brand, '#123524');
  assert.equal(COLOURS.brandDeep, '#0B271A');
  assert.equal(COLOURS.brandRaised, '#244838');
  assert.equal(COLOURS.brandMid, '#3F5E4E');
  assert.equal(COLOURS.accent, '#43D9B1');
  assert.equal(COLOURS.accentHover, '#2FBF9D');
  assert.equal(COLOURS.sageStone, '#D8E2DC');
  assert.equal(COLOURS.mist, '#F4F7F5');
  assert.equal(COLOURS.white, '#FFFFFF');
});

test('dark chart sequence avoids Phthalo Green as an unoutlined series', () => {
  assert.deepEqual(CHART_SEQUENCE.slice(0, 10), [
    '#43D9B1', '#F28C28', '#A889FF', '#D6B84C', '#4FA3D9',
    '#F06DB2', '#8BC34A', '#F2CF3A', '#F39A78', '#E54865',
  ]);
});

test('graph colour maps are populated from the new palette', async () => {
  assert.ok(Object.keys(EDGE_COLOURS).length > 20);
  assert.equal(MATERIAL_COLOURS.rule, COLOURS.brandRaised);
  const source = await readFile(new URL('./main.jsx', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /const (EDGE_COLOURS|MATERIAL_COLOURS|CLUSTER_COLOURS)\s*=/);
  assert.doesNotMatch(source, /#2457d6|#2563eb|#0f766e/iu);
  assert.match(source, /function drawCanvasLabel[\s\S]*?ctx\.fillStyle='rgba\(255,255,255,\.95\)'/u);
  assert.match(source, /function drawCanvasLabel[\s\S]*?ctx\.fillStyle=COLOURS\.brand/u);
});

test('DOM styles expose the approved shared tokens and focus treatment', async () => {
  const source = await readFile(new URL('./styles.css', import.meta.url), 'utf8');
  assert.match(source, /--colour-brand:\s*#123524/iu);
  assert.match(source, /--colour-accent:\s*#43D9B1/iu);
  assert.match(source, /--colour-page:\s*#F4F7F5/iu);
  assert.match(source, /--colour-border:\s*#D8E2DC/iu);
  assert.match(source, /focus-visible[\s\S]*outline:\s*2px solid var\(--colour-focus\)/iu);
});

test('reader styles no longer use the old sepia palette', async () => {
  const source = await readFile(new URL('./styles.css', import.meta.url), 'utf8');
  assert.match(source, /--reader-paper:\s*var\(--colour-surface\)/iu);
  assert.match(source, /--reader-ink:\s*var\(--colour-brand\)/iu);
  const readerSource = source.slice(source.indexOf('/* Legal provision reading mode */'));
  assert.doesNotMatch(readerSource, /#fbf8f1|#f4efe6|#f7f1e7|#f2e8d8|#f5efe5|#f2ebdf|#eee8de|#fbf8f2|#795c34|#eee1ca|#7b5b35/iu);
});

function lastRuleBody(source, selector) {
  const matches = [...source.matchAll(new RegExp(`(?:^|\\n)\\s*${selector}\\s*\\{([^}]*)\\}`, 'g'))];
  return matches.at(-1)?.[1] || '';
}

function lastRuleContaining(source, selector, property) {
  const matches = [...source.matchAll(new RegExp(`(?:^|\\n)\\s*${selector}\\s*\\{([^}]*)\\}`, 'g'))];
  return matches.map(match => match[1]).reverse().find(body => body.includes(property)) || '';
}

test('image-reference workspace keeps a light workbench around the dark rail', async () => {
  const source = await readFile(new URL('./styles.css', import.meta.url), 'utf8');
  assert.match(lastRuleBody(source, '\\.shell'), /grid-template-columns:\s*224px\s+minmax\(0,1fr\)\s+370px/iu);
  assert.match(lastRuleContaining(source, '\\.topbar', 'background'), /background:\s*var\(--colour-surface\)/iu);
  assert.match(lastRuleContaining(source, '\\.rail', 'background'), /background:\s*var\(--colour-brand\)/iu);
  assert.match(lastRuleContaining(source, '\\.canvas', 'background'), /background:\s*var\(--colour-page\)/iu);
  assert.match(lastRuleContaining(source, '\\.canvas-meta', 'background'), /background:\s*rgba\(255,255,255,\.95\)/iu);
  assert.match(lastRuleContaining(source, '\\.reporting-view', 'background'), /background:\s*var\(--colour-page\)/iu);
  assert.match(lastRuleContaining(source, '\\.reporting-graph-nav', 'background'), /background:\s*var\(--colour-surface\)/iu);
  assert.match(lastRuleContaining(source, '\\.reference-shelf', 'background'), /background:\s*var\(--colour-surface\)/iu);
  assert.match(lastRuleContaining(source, '\\.reference-shelf-card', 'background'), /background:\s*var\(--colour-surface\)/iu);
});
