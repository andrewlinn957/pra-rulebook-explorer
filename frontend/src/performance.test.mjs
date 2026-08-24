import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');

test('initial rulebook catalogue requests compact part summaries', () => {
  assert.match(source, /api\('\/nodes\?types=part&limit=300&summary=true'\)/);
});

test('selecting a node leaves neighbourhood loading to the selected-state effect', () => {
  const chooseBody = source.slice(
    source.indexOf('async function choose'),
    source.indexOf('function goUp'),
  );

  assert.doesNotMatch(chooseBody, /loadNeighbourhood\(full\.id\)/);
  assert.match(source, /loadNeighbourhood\(selected\.id\)/);
});

test('bootstrap does not block the catalogue on statistics or root fallback data', () => {
  const bootstrapBody = source.slice(
    source.indexOf('async function bootstrap'),
    source.indexOf('async function loadAllParts'),
  );

  assert.match(bootstrapBody, /const statsPromise=api\('\/stats'\)/);
  assert.match(bootstrapBody, /const parts=await api\('\/nodes\?types=part&limit=300&summary=true'\)/);
  assert.match(bootstrapBody, /statsPromise\.catch/);
});
