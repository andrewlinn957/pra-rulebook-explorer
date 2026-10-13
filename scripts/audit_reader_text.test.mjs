import assert from 'node:assert/strict';
import { test } from 'node:test';
import { checkText } from './audit_reader_text.mjs';

test('preserved source lists and whitespace do not count as text loss', () => {
  const result = checkText({ id: 's', text: '(1) First;\n(2) Second.', metadata: {} });
  assert.equal(result.matches, true);
});

test('the audit detects dropped wording even when the remainder is readable', () => {
  const text = 'Comply with rule 2.3 and Article 4.5 of the Rulebook.';
  const result = checkText({ id: 's', text, metadata: {} }, () => [{text: '5 of the Rulebook.'}]);
  assert.equal(result.matches, false);
  assert.ok(result.rendered_length < result.source_length);
});
