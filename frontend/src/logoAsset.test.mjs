import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { test } from 'node:test';

const mainSource = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const logoUrl = new URL('../public/pra-rulebook-graph-logo.svg', import.meta.url);

test('top-left rail uses the scalable graph logo asset', () => {
  assert.match(mainSource, /pra-rulebook-graph-logo\.svg/u);
  assert.equal(existsSync(logoUrl), true);

  const svg = readFileSync(logoUrl, 'utf8');
  assert.match(svg, /viewBox="0 0 64 64"/u);
  assert.match(svg, /<circle/gu);
  assert.match(svg, /<path/gu);
  assert.doesNotMatch(svg, /<text/iu);
});
