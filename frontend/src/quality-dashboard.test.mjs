import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

test('quality tab is a queue-first workbench rather than a dashboard of cards', () => {
  assert.match(source, /className="quality quality-workbench"/);
  assert.match(source, /quality-queue-rail/);
  assert.match(source, /FeedbackQueueWorksurface/);
  assert.match(source, /UnverifiedLinksWorksurface/);
  assert.match(source, /Process queue/);
  assert.match(source, /Save finding/);
  assert.doesNotMatch(source, /Can I trust the explorer\?/);
  assert.doesNotMatch(source, /What needs attention/);
  assert.doesNotMatch(source, /quality-redesign/);
  assert.doesNotMatch(source, /quality-evidence-drawer/);
  assert.doesNotMatch(source, /audit-cockpit/);
});

test('quality workbench styles reserve most of the screen for the workflow', () => {
  assert.match(styles, /\.quality-workbench\{[^}]*padding:8px 14px 14px/);
  assert.match(styles, /\.quality-workspace\{[^}]*grid-template-columns:124px minmax\(0,1fr\)/);
  assert.match(styles, /\.quality-queue-rail/);
  assert.match(styles, /\.quality-workflow/);
  assert.match(styles, /\.links-workgrid/);
  assert.doesNotMatch(styles, /\.quality-redesign/);
  assert.doesNotMatch(styles, /\.quality-evidence-drawer/);
  assert.doesNotMatch(styles, /\.audit-cockpit/);
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
  assert.match(source, /function reportingSourceLinkLabel\(node\)/);
  assert.match(source, /function sourceFileName\(value\)/);
  assert.doesNotMatch(source, /local degree/);
});

test('reporting drilldown rail is contextual rather than useless summary counts', () => {
  assert.doesNotMatch(source, /className="reporting-stats"/);
  assert.match(source, /function ReportingRail\(\{roots,selectedReturn,detail,graph,onOpen,onDrill,onBackToOverview\}\)/);
  assert.match(source, /Back to returns overview/);
  assert.match(source, /Back to return/);
  assert.match(source, /Sample datapoints/);
});

test('reporting overview rail groups returns by reporting estate', () => {
  assert.match(source, /function groupReportingReturns\(roots\)/);
  assert.match(source, /function reportingEstateForReturn\(node\)/);
  assert.match(source, /COREP returns/);
  assert.match(source, /PRA returns/);
  assert.match(source, /function compareReturnCode\(a,b\)/);
  assert.match(source, /className="reporting-return-groups"/);
  assert.match(styles, /\.reporting-return-group h4/);
});

test('reporting graph keeps return as selected graph root while inspecting child nodes', () => {
  assert.match(source, /const reportingRoot=useMemo/);
  assert.match(source, /selected=\{reportingRoot\} detail=\{detail\}/);
  assert.match(source, /function reportingMaterialFilters\(graph\)/);
});

test('reporting graph distinguishes templates instructions and XBRL sources visually', () => {
  assert.match(source, /function reportingVisualKind\(node\)/);
  assert.match(source, /visual==='template'/);
  assert.match(source, /visual==='instruction'/);
  assert.match(source, /visual==='xbrl_source'/);
  assert.match(source, /function isXbrlSourceDocument\(n\)/);
  assert.match(source, /reporting_xbrl_source:'XBRL source'/);
  assert.match(styles, /\.legend i\.legend-node\.template/);
  assert.match(styles, /\.legend i\.legend-node\.instruction/);
  assert.match(styles, /\.legend i\.legend-node\.xbrl-source/);
});


test('node feedback result can expand to the full saved output', () => {
  assert.match(source, /function ExpandableResult\(/);
  assert.match(source, /Show full result/);
  assert.match(source, /Hide full result/);
  assert.match(source, /className=\{`result-cell \$\{open\?'open':'collapsed'\}`\}/);
  assert.match(styles, /\.result-cell\.collapsed small\{[^}]*max-height:54px/);
  assert.match(styles, /\.result-cell\.open small\{[^}]*max-height:none/);
});

test('unverified link review captures actionable findings without nested workflows', () => {
  assert.match(source, /function UnverifiedLinksWorksurface/);
  assert.match(source, /Resolved/);
  assert.match(source, /External valid/);
  assert.match(source, /Broken/);
  assert.match(source, /Not a link/);
  assert.match(source, /Not relevant/);
  assert.match(source, /No longer valid/);
  assert.match(source, /Rulebook target, if resolved internally/);
  assert.match(source, /Replacement URL, if needed/);
  assert.match(source, /Short finding/);
  assert.doesNotMatch(source, /function UnresolvedLinkReview/);
  assert.doesNotMatch(source, /action-queue-grid/);
});
