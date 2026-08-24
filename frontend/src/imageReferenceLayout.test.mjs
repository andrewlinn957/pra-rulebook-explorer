import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const mainSource = await readFile(new URL('./main.jsx', import.meta.url), 'utf8');
const styleSource = await readFile(new URL('./styles.css', import.meta.url), 'utf8');

function lastRuleBody(source, selector) {
  const matches = [...source.matchAll(new RegExp(`(?:^|\\n)\\s*${selector}\\s*\\{([^}]*)\\}`, 'g'))];
  return matches.at(-1)?.[1] || '';
}

function lastRuleContaining(source, selector, property) {
  const matches = [...source.matchAll(new RegExp(`(?:^|\\n)\\s*${selector}\\s*\\{([^}]*)\\}`, 'g'))];
  return matches.map(match => match[1]).reverse().find(body => body.includes(property)) || '';
}

test('image-reference desktop shell uses the reference proportions', () => {
  const shell = lastRuleBody(styleSource, '\\.shell');
  const openShell = lastRuleBody(styleSource, '\\.shell\\.panel-open');
  const topbar = lastRuleContaining(styleSource, '\\.topbar', 'background');

  assert.match(shell, /grid-template-columns:\s*224px\s+minmax\(0,1fr\)\s+370px/iu);
  assert.match(shell, /grid-template-rows:\s*58px\s+minmax\(0,1fr\)/iu);
  assert.match(openShell, /grid-template-rows:\s*58px\s+minmax\(0,1fr\)/iu);
  assert.match(topbar, /background:\s*var\(--colour-surface\)/iu);
});

test('image-reference workspace exposes a branded rail and light work surfaces', () => {
  assert.match(mainSource, /className="rail-brand"/u);
  assert.match(mainSource, /className="rail-brand-mark"/u);
  assert.match(mainSource, /className="rail-collapse"/u);
  assert.match(mainSource, /className="rail-utilities"/u);
  assert.match(mainSource, /className="inspector-tabs"/u);

  assert.match(lastRuleBody(styleSource, '\\.rail'), /background:\s*var\(--colour-brand\)/iu);
  assert.match(lastRuleBody(styleSource, '\\.canvas'), /background:\s*var\(--colour-page\)/iu);
  assert.match(lastRuleBody(styleSource, '\\.inspector'), /background:\s*var\(--colour-surface\)/iu);
  assert.match(lastRuleContaining(styleSource, '\\.canvas-meta', 'background'), /background:\s*rgba\(255,255,255,\.95\)/iu);
});

test('image-reference desktop panel opens above the mobile threshold', () => {
  assert.match(mainSource, /useState\(\(\)=>window\.innerWidth>900\)/u);
  assert.match(mainSource, /const initialNode=parts\.results\?\.\[0\]\|\|roots\.results\?\.\[0\]/u);
});
