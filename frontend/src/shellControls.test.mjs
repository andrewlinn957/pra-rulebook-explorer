import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { test } from 'node:test';

const mainSource = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styleSource = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');
const headerSource = mainSource.slice(mainSource.indexOf('<header className="topbar">'), mainSource.indexOf('</header>') + '</header>'.length);
const railSource = mainSource.slice(mainSource.indexOf('<aside className="rail">'), mainSource.indexOf('</aside>') + '</aside>'.length);
const reportingSource = mainSource.slice(mainSource.indexOf('function ReportingGraphView'), mainSource.indexOf('function ReportingImpactExplorer'));

test('settings and help live in the bottom-left rail utilities', () => {
  assert.doesNotMatch(headerSource, /className="settings(?:\s|")/u);
  assert.match(railSource, /<details className="settings rail-settings"/u);
  assert.match(railSource, /href="help\.html"/u);
  assert.doesNotMatch(railSource, /Leave PRA Rulebook/u);
  assert.match(mainSource, /Graph settings/u);
  assert.match(mainSource, /Link origin/u);
  assert.match(mainSource, /Relationship edges/u);
});

test('the graph title bar no longer exposes expand graph', () => {
  assert.doesNotMatch(mainSource, /className="expand-graph"/u);
  assert.doesNotMatch(mainSource, /graphExpanded/u);
});

test('reporting keeps the catalogue rail flush with the graph canvas', () => {
  assert.match(reportingSource, /<aside className="reporting-catalog-list reporting-graph-nav">/u);
  assert.match(reportingSource, /<main className="reporting-graph-canvas">/u);
  assert.match(styleSource, /\.reporting-graph-layout\{grid-template-columns:258px minmax\(0,1fr\) 326px\}/u);
  assert.match(styleSource, /@media\(max-width:1320px\) and \(min-width:981px\)\{[\s\S]*?\.reporting-graph-layout\{grid-template-columns:226px minmax\(0,1fr\) 292px\}/u);
  assert.doesNotMatch(styleSource, /\.reporting-graph-layout\{grid-template-columns:minmax\(0,1fr\) 326px\}/u);
  assert.match(styleSource, /\.shell\.reporting-view-mode,\s*\.shell\.reporting-view-mode\.panel-open,\s*\.shell\.reporting-view-mode\.panel-closed\s*\{\s*grid-template-columns:1fr/u);
});

test('reporting cells and impact views retain their number formatter', () => {
  assert.match(mainSource, /function fmt\(v\)\{return typeof v==='number'\?v\.toLocaleString\(undefined,\{maximumFractionDigits:3\}\):v\}/u);
  assert.match(reportingSource, /<ReportingCellExplorer/u);
  assert.match(mainSource, /function ReportingImpactResults/u);
});

test('the linked help page covers the active app workflows', () => {
  const helpUrl = new URL('../public/help.html', import.meta.url);
  assert.equal(existsSync(helpUrl), true);
  const help = readFileSync(helpUrl, 'utf8');
  for (const heading of ['Search and navigate', 'Use the graph', 'Inspect a node', 'Use reporting', 'Read a provision']) {
    assert.match(help, new RegExp(heading, 'u'));
  }
});
