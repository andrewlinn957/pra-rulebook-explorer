import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

function lastRuleBody(sourceText, selector) {
  const matches = [...sourceText.matchAll(new RegExp(`(?:^|\\n)\\s*${selector}\\s*\\{([^}]*)\\}`, 'g'))];
  return matches.at(-1)?.[1] || '';
}

test('rail result rows use a fixed rhythm independent of title length', () => {
  const resultStack = lastRuleBody(styles, '\\.result-stack');
  const hit = lastRuleBody(styles, '\\.hit');

  assert.match(resultStack, /grid-auto-rows:\s*30px/iu);
  assert.match(hit, /height:\s*30px/iu);
  assert.match(hit, /overflow:\s*hidden/iu);
});

test('inspector defaults to selected node and keeps the action affordances there', () => {
  const detailSource = source.slice(source.indexOf('function SelectedNodeDetails'), source.indexOf('function NodeTitle'));
  assert.match(source, /const \[inspectorTab,setInspectorTab\]=useState\('selected-node'\)/u);
  assert.match(source, /role="tab"/u);
  assert.match(source, /setInspectorTab\('connections'\)/u);
  assert.match(source, /setInspectorTab\('selected-node'\)/u);
  assert.match(source, /aria-selected=\{inspectorTab==='selected-node'\}/u);
  assert.match(source, /<ConnectionsOverview /u);
  assert.match(source, /<SelectedNodeDetails /u);
  assert.match(source, /function SelectedNodeDetails\(/u);
  assert.match(detailSource, /Open source ↗/u);
  assert.match(detailSource, /className="reading-mode-entry"/u);
  assert.match(detailSource, /className="report-issue-btn"/u);
  assert.doesNotMatch(detailSource, /Cross-references/u);
  assert.doesNotMatch(detailSource, /Visible connections/u);
});

test('connections tab owns cross-reference and visible-connection detail', () => {
  const connectionsSource = source.slice(source.indexOf('function ConnectionsOverview'), source.indexOf('function SelectedNodeDetails'));
  assert.match(connectionsSource, /Cross-references/u);
  assert.match(connectionsSource, /title="Visible connections"/u);
  assert.match(connectionsSource, /className="edge-list"/u);
  assert.match(connectionsSource, /onChoose\(other\)/u);
});
