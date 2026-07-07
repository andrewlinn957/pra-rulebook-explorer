import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

test('quality tab is a plain-English issue review rather than an audit cockpit', () => {
  assert.match(source, /className="quality quality-redesign"/);
  assert.match(source, /What needs attention/);
  assert.match(source, /Can I trust the explorer\?/);
  assert.match(source, /What this means/);
  assert.match(source, /Why it matters/);
  assert.match(source, /What to do next/);
  assert.match(source, /Show evidence/);
  assert.doesNotMatch(source, /Risk<\/span>/);
  assert.doesNotMatch(source, /audit-cockpit/);
  assert.doesNotMatch(source, /Priority<\/span>/);
});

test('quality redesign styles make issue cards and evidence drawers first-class', () => {
  assert.match(styles, /\.quality-redesign/);
  assert.match(styles, /\.quality-summary-grid/);
  assert.match(styles, /\.quality-issue-card/);
  assert.match(styles, /\.quality-evidence-drawer/);
});

test('reporting tab uses compact side panels around the graph', () => {
  assert.match(styles, /\.reporting-layout\{[^}]*grid-template-columns:248px minmax\(0,1fr\) 320px/);
  assert.match(styles, /\.reporting-toolbar\{[^}]*padding:8px 10px/);
  assert.match(styles, /\.reporting-rail\{[^}]*padding:8px/);
  assert.match(styles, /\.reporting-nav-back\{/);
});

test('reporting graph navigation and inspector match the main graph conventions', () => {
  assert.match(source, /function inspectReportingNode\(node\)/);
  assert.match(source, /function drillReportingNode\(node\)/);
  assert.match(source, /onSelect=\{inspectReportingNode\} onOpen=\{drillReportingNode\}/);
  assert.match(source, /Click to inspect · double-click to open\/drill/);
  assert.match(source, /Drag to pan · scroll to zoom · click to inspect · double-click to open/);
});

test('reporting inspector opens with understandable metadata and original data links', () => {
  assert.match(source, /function ReportingMetadata\(\{node,edges,graph\}\)/);
  assert.match(source, /Useful links/);
  assert.match(source, /reportingMetadataRows\(node\)/);
  assert.match(source, /reportingUsefulLinks\(node,edges,graph\)/);
  assert.match(source, /Data item code/);
  assert.match(source, /Open source/);
  assert.doesNotMatch(source, /local degree/);
});

test('reporting drilldown rail is contextual rather than useless summary counts', () => {
  assert.doesNotMatch(source, /className="reporting-stats"/);
  assert.match(source, /function ReportingRail\(\{roots,selectedReturn,detail,graph,onOpen,onDrill,onBackToOverview\}\)/);
  assert.match(source, /Back to returns overview/);
  assert.match(source, /Back to return/);
  assert.match(source, /Sample datapoints/);
});

test('reporting graph keeps return as selected graph root while inspecting child nodes', () => {
  assert.match(source, /const reportingRoot=useMemo/);
  assert.match(source, /selected=\{reportingRoot\} detail=\{detail\}/);
  assert.match(source, /function reportingMaterialFilters\(graph\)/);
});

test('unresolved link review captures actionable findings', () => {
  assert.match(source, /Review finding/);
  assert.match(source, /function UnresolvedLinkReview/);
  assert.match(source, /URL works but points to an out-of-date document/);
  assert.match(source, /URL works but irrelevant/);
  assert.match(source, /URL is dead/);
  assert.match(source, /Link should point to an existing Rulebook page\/provision/);
  assert.match(source, /Keep as external reference/);
  assert.match(source, /Correct URL/);
  assert.match(source, /Correct Rulebook page or provision/);
  assert.doesNotMatch(source, /reviewed by/i);
});
