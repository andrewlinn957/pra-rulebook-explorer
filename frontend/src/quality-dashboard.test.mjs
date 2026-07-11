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

test('reporting inspector opens with a URLs card and minimal details', () => {
  assert.match(source, /function ReportingMetadata\(\{node,edges,graph\}\)/);
  assert.match(source, /title="URLs"/);
  assert.match(source, /reportingMetadataRows\(node\)/);
  assert.match(source, /reportingSourceUrls\(node,edges,graph\)/);
  assert.match(source, /Data item code/);
  assert.match(source, /Reporting domain/);
  assert.match(source, /Template explanation/);
  assert.match(source, /Open source/);
  assert.match(source, /function reportingSourceLinkLabel\(node\)/);
  assert.match(source, /function sourceFileName\(value\)/);
  assert.doesNotMatch(source, /Useful links/);
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
  assert.doesNotMatch(source, /reportingReturnSummary\(n\)/);
  assert.doesNotMatch(source, /function reportingReturnSummary\(node\)/);
  assert.match(styles, /\.reporting-return-group h4/);
});

test('reporting drilldown rail groups related artefacts by role', () => {
  assert.match(source, /function reportingRailGroups\(node,graph\)/);
  assert.match(source, /Templates/);
  assert.match(source, /Instructions and guidance/);
  assert.match(source, /Taxonomies/);
  assert.match(source, /Rules and legal basis/);
  assert.match(source, /Concepts and scope/);
  assert.match(source, /group\.items\.map/);
});

test('reporting rail keeps descriptions out of navigation rows', () => {
  assert.doesNotMatch(source, /reportingReturnSummary\(n\)/);
  assert.doesNotMatch(source, /group\.items\.map\(n=>.*reportingNodeSummary\(n\)/s);
  assert.match(source, /add\('template_summary','Template explanation'\)/);
});

test('reporting inspector shows rich user-facing template details without LLM plumbing or audit metadata', () => {
  assert.match(source, /title="URLs"/);
  assert.match(source, /function reportingSourceUrls\(node,edges,graph\)/);
  assert.match(source, /!\['USES_TEMPLATE','USES_INSTRUCTIONS','EVIDENCED_BY'\]\.includes/);
  assert.match(source, /add\('template_code','Template code'\)/);
  assert.match(source, /add\('annex','Annex'\)/);
  assert.match(source, /add\('template_summary','Template explanation'\)/);
  assert.match(source, /add\('template_quality_notes','Quality notes'\)/);
  assert.match(source, /add\('source_title','Source title'\)/);
  assert.match(source, /add\('data_item_code','Data item code'\)/);
  assert.match(source, /add\('reporting_domain','Reporting domain'\)/);
  assert.doesNotMatch(source, /audit_cleanup/);
  assert.doesNotMatch(source, /addAuditCleanupRows/);
  assert.doesNotMatch(source, /add\('template_enrichment_model'/);
  assert.doesNotMatch(source, /add\('template_enrichment_prompt_version'/);
  assert.doesNotMatch(source, /add\('template_enrichment_input_hash'/);
});

test('reporting rail groups source artefacts by useful file category and dedupes URLs', () => {
  assert.match(source, /label:'Templates',match:n=>/);
  assert.match(source, /label:'Instructions and guidance',match:n=>/);
  assert.match(source, /label:'Taxonomies',match:n=>/);
  assert.match(source, /function reportingRailDedupeKey\(node\)/);
  assert.match(source, /function normaliseSourceUrl\(url\)/);
  assert.match(source, /function compareReportingRailCandidates\(a,b\)/);
  assert.match(source, /node\?\.node_type==='InstructionSet'\) return 2/);
  assert.doesNotMatch(source, /isTaxonomySourceDocument\(n\)\|\|n\.node_type==='TemplateSet'/);
});

test('reporting graph keeps return as selected graph root while inspecting child nodes', () => {
  assert.match(source, /const reportingRoot=useMemo/);
  assert.match(source, /selected=\{reportingRoot\} detail=\{detail\}/);
  assert.match(source, /function reportingMaterialFilters\(graph\)/);
});

test('reporting drilldown resets filters and warns when filters hide graph content', () => {
  assert.match(source, /function resetReportingFilters\(\)/);
  assert.match(source, /resetReportingFilters\(\);\s*\n\s*await loadReportingGraph\('', returnCode\(node\), \{resetFilters:false\}\)/);
  assert.match(source, /const hiddenByFilters=useMemo/);
  assert.match(source, /Some reporting nodes or links are hidden by filters/);
  assert.match(source, /Reset filters/);
  assert.match(styles, /\.reporting-filter-notice/);
});

test('reporting rail dedupes source documents by URL without hiding parsed templates', () => {
  assert.match(source, /if\(node\?\.node_type==='Template'\) return `template:\$\{node\.id\}`/);
  assert.match(source, /if\(node\?\.node_type==='TemplateSet'\) return `template-set:\$\{node\.id\}`/);
  assert.match(source, /return `url:\$\{normaliseSourceUrl\(url\)\}`/);
});

test('reporting template inspector opens details by default', () => {
  assert.match(source, /const openDetails=node\?\.node_type==='Template' \|\| rows\.length<=6/);
  assert.match(source, /<Collapsible title="Details" count=\{`\$\{rows\.length\} fields`\} open=\{openDetails\}>/);
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

test('React root is reused across dev-server reloads', () => {
  assert.match(source, /const appContainer=document\.getElementById\('root'\)/);
  assert.match(source, /appContainer\.__praRulebookRoot/);
  assert.match(source, /\(appContainer\.__praRulebookRoot\?\?=createRoot\(appContainer\)\)\.render\(<App\/>\)/);
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
