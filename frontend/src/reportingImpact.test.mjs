import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');

describe('reporting change impact', () => {
  it('offers impact analysis as a reporting surface', () => {
    assert.match(source, />Change impact</);
    assert.match(source, /function ReportingImpactExplorer/);
    assert.match(source, /\/reporting\/impact\//);
  });

  it('keeps direct coordinates separate from candidate cells', () => {
    assert.match(source, /same instruction passage names this rule/);
    assert.match(source, /Direct coordinate evidence/);
    assert.match(source, /Instruction-defined coordinate/);
    assert.match(source, /remaining templates and cells are candidate scope/);
    assert.match(source, /review scope, not confirmed edits/);
  });

  it('shows evidence passages and official instruction sources', () => {
    assert.match(source, /Reference evidence/);
    assert.match(source, /Instruction sources/);
    assert.match(source, /ref\.evidence_text/);
    assert.match(source, /source\.url/);
  });
});
