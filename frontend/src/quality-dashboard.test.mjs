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
  assert.doesNotMatch(source, /Process queue/);
  assert.doesNotMatch(source, /\/feedback\/process/);
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

test('reporting tab uses the graph canvas with navigation and information side panels', () => {
  assert.match(styles, /\.reporting-graph-layout\{[^}]*grid-template-columns:290px minmax\(0,1fr\) 340px/);
  assert.match(styles, /\.reporting-toolbar\{[^}]*padding:8px 10px/);
  assert.match(source, /className="reporting-catalog-list reporting-graph-nav"/);
  assert.match(source, /className="reporting-graph-canvas"/);
  assert.match(source, /className="reporting-graph-inspector"/);
  assert.match(source, /<Graph graph=\{activeGraph\}/);
});

test('a single click on a reporting overview node loads its child navigation', () => {
  assert.match(source, /onSelect=\{openGraphNode\} onOpen=\{openGraphNode\}/);
  assert.match(source, /if\(!selectedId && node\?\.metadata\?\.return_id\)/);
  assert.match(source, /<ReportingChildNavigation node=\{nodeDetail\}/);
});

test('reporting graph keeps overview structure but hides it inside a selected requirement', () => {
  assert.match(source, /reportingOneHopGraph\(filtered,nodeDetail\?\.id\)/);
  assert.match(source, /new Set\(REPORTING_OVERVIEW_EDGE_GROUP_KEYS\)/);
  assert.match(source, /new Set\(REPORTING_REQUIREMENT_EDGE_GROUP_KEYS\)/);
  assert.match(source, /reportingRequirementEditions\(selectedRow,catalog\.returns\|\|\[\]\)/);
  assert.match(source, /className="reporting-edition-switcher"/);
  assert.match(source, /relationshipFilters=\{REPORTING_EDGE_GROUPS\.map/);
  assert.doesNotMatch(source, /relationshipFilters=\{REPORTING_EDGE_TYPES\}/);
});

test('reporting catalogue separates returns from Pillar 3 disclosures', () => {
  assert.match(source, /setEstate\('supervisory_reporting'\)/);
  assert.match(source, /setEstate\('pillar3_disclosure'\)/);
  assert.match(source, /Regulatory returns/);
  assert.match(source, />Pillar 3</);
  assert.match(source, /disclosure sets/);
});

test('reporting inspector opens with a URLs card and minimal details', () => {
  assert.match(source, /function ReportingGraphInfo\(\{node,catalogDetail,edges,graph,onSelect,onFeedback\}\)/);
  assert.match(source, /title="Source files"/);
  assert.match(source, /reportingSourceUrls\(node,edges,graph\)/);
  assert.match(source, /Connected nodes/);
  assert.match(source, /Flag an issue with this node/);
  assert.match(source, /Open source/);
  assert.match(source, /function reportingSourceLinkLabel\(node\)/);
  assert.match(source, /function sourceFileName\(value\)/);
  assert.doesNotMatch(source, /function ReportingMetadata\(\{node,edges,graph\}\)/);
  assert.doesNotMatch(source, /Useful links/);
  assert.doesNotMatch(source, /local degree/);
});

test('reporting drilldown navigation uses the active child-navigation component', () => {
  assert.doesNotMatch(source, /className="reporting-stats"/);
  assert.match(source, /function ReportingChildNavigation\(\{node,root,groups,parents,onSelect\}\)/);
  assert.match(source, /Return root/);
  assert.match(source, /Child nodes/);
  assert.match(source, /This is a leaf node/);
  assert.doesNotMatch(source, /function ReportingRail\(/);
  assert.doesNotMatch(source, /Back to returns overview/);
  assert.doesNotMatch(source, /Sample datapoints/);
});

test('reporting overview groups catalog rows by official collection', () => {
  assert.match(source, /const families=useMemo/);
  assert.match(source, /row\.collection_name\|\|row\.family/);
  assert.match(source, /className="reporting-catalog-list reporting-graph-nav"/);
  assert.match(source, /'editions'/);
  assert.match(source, /node_type:'ReportingCollection'/);
  assert.match(source, /edge_type:'HAS_EDITION'/);
  assert.match(source, /reportingOneHopGraph\(graph,nodeDetail\.id\)/);
  assert.match(source, /focusCollection\(group\.name\)/);
  assert.doesNotMatch(source, /function groupReportingReturns\(roots\)/);
  assert.doesNotMatch(source, /function reportingEstateForReturn\(node\)/);
  assert.doesNotMatch(source, /function compareReturnCode\(a,b\)/);
  assert.doesNotMatch(source, /className="reporting-return-groups"/);
  assert.doesNotMatch(source, /reportingReturnSummary\(n\)/);
  assert.doesNotMatch(source, /function reportingReturnSummary\(node\)/);
  assert.match(styles, /\.reporting-catalog-list h3/);
});

test('reporting child navigation groups related artefacts by relationship role', () => {
  assert.match(source, /reportingChildGroups\(nodeDetail,activeGraph\)/);
  assert.match(source, /reportingParentNodes\(nodeDetail,activeGraph\)/);
  assert.match(source, /REPORTING_EDGE_GROUPS\.map/);
  assert.match(source, /group\.children\.map/);
  assert.doesNotMatch(source, /function reportingRailGroups\(node,graph\)/);
  assert.doesNotMatch(source, /Rules and legal basis/);
  assert.doesNotMatch(source, /Concepts and scope/);
});

test('reporting child navigation keeps descriptions out of navigation rows', () => {
  assert.doesNotMatch(source, /reportingReturnSummary\(n\)/);
  assert.doesNotMatch(source, /group\.items\.map\(n=>.*reportingNodeSummary\(n\)/s);
  assert.doesNotMatch(source, /function reportingNodeSummary\(node\)/);
});

test('reporting inspector shows rich user-facing template details without LLM plumbing or audit metadata', () => {
  assert.match(source, /title="Source files"/);
  assert.match(source, /function reportingSourceUrls\(node,edges,graph\)/);
  assert.match(source, /function ReportingInfoLinks\(\{title,items\}\)/);
  assert.match(source, /item\.resolved_display_name\|\|item\.display_title/);
  assert.match(source, /item\.sheet_names/);
  assert.match(source, /Connected nodes/);
  assert.doesNotMatch(source, /audit_cleanup/);
  assert.doesNotMatch(source, /addAuditCleanupRows/);
  assert.doesNotMatch(source, /add\('template_enrichment_model'/);
  assert.doesNotMatch(source, /add\('template_enrichment_prompt_version'/);
  assert.doesNotMatch(source, /add\('template_enrichment_input_hash'/);
});

test('reporting source links classify useful file categories without the old rail helpers', () => {
  assert.match(source, /function reportingUrlKind\(node\)/);
  assert.match(source, /Template workbook/);
  assert.match(source, /Instructions and guidance/);
  assert.match(source, /Taxonomy/);
  assert.doesNotMatch(source, /function reportingRailDedupeKey\(node\)/);
  assert.doesNotMatch(source, /function normaliseSourceUrl\(url\)/);
  assert.doesNotMatch(source, /function compareReportingRailCandidates\(a,b\)/);
  assert.doesNotMatch(source, /isTaxonomySourceDocument\(n\)\|\|n\.node_type==='TemplateSet'/);
});

test('reporting catalogue exposes templates, workbook sheets, instructions and Rulebook references', () => {
  assert.match(source, /function ReportingGraphInfo\(\{node,catalogDetail,edges,graph,onSelect,onFeedback\}\)/);
  assert.match(source, /function ReportingInfoLinks\(\{title,items\}\)/);
  assert.match(source, /item\.sheet_names/);
  assert.match(source, /title="Instructions"/);
  assert.match(source, /Connected nodes/);
});

test('cell explorer renders the selected template as a coordinate matrix rather than a flat ledger', () => {
  assert.match(source, /function ReportingTemplateMatrix\(/);
  assert.match(source, /className="reporting-template-grid"/);
  assert.match(source, /reportingTemplateGrid\(data\.cells\|\|\[\],query\)/);
  assert.match(source, /pageSize=500/);
  assert.match(source, /gridRef\.current\.scrollTo\(\{top:0,left:0\}\)/);
  assert.doesNotMatch(source, /className="reporting-cell-ledger"/);
  assert.doesNotMatch(source, /className="reporting-cell-pages"/);
  assert.match(styles, /\.reporting-template-grid thead th\{[^}]*position:sticky/);
  assert.match(styles, /\.reporting-template-grid tbody th\{[^}]*position:sticky;left:0/);
  assert.match(source, /function ReportingWorkbookTemplate\(/);
  assert.match(source, /reportingWorkbookCellStyle\(layout\.styles\?\.\[cell\.style_id\]/);
  assert.match(source, /data\.layout\?\.format==='pdf'/);
  assert.match(source, /function ReportingPdfTemplate\(/);
  assert.match(source, /\/document#page=1&toolbar=0/);
  assert.match(styles, /\.reporting-workbook-sheet\{/);
  assert.match(styles, /\.reporting-pdf-template iframe\{/);
});

test('cell explorer carries the graph-selected template into the cells view', () => {
  assert.match(source, /reportingTemplateForNode\(preferredNode,summary\.templates\)/);
  assert.match(source, /reportingTemplateForNode\(nodeDetail,cellData\?\.templates\)/);
  assert.match(source, /loadCells\(\{template:selectedTemplate,preferredNode:nodeDetail\}\)/);
  assert.match(source, /coverage:'selected_template_unavailable'/);
  assert.match(source, /reportingNodeSelectsTemplate\(nodeDetail\)&&!selectedTemplate/);
});

test('reporting catalogue keeps the legacy public return page removed', () => {
  assert.doesNotMatch(source, /function ReportingReturnPage\(/);
  assert.doesNotMatch(source, /function ReportingArtifactCard\(/);
  assert.doesNotMatch(source, /function ReportingReference\(/);
  assert.doesNotMatch(source, /function dedupeReportingReferences\(/);
  assert.match(source, /Open official file/);
  assert.doesNotMatch(source, /View this return in the official PRA reporting catalogue/);
});

test('legacy reporting rail source-dedupe helpers are removed', () => {
  assert.doesNotMatch(source, /if\(node\?\.node_type==='Template'\) return `template:\$\{node\.id\}`/);
  assert.doesNotMatch(source, /if\(node\?\.node_type==='TemplateSet'\) return `template-set:\$\{node\.id\}`/);
  assert.doesNotMatch(source, /return `url:\$\{normaliseSourceUrl\(url\)\}`/);
});

test('legacy reporting metadata-details panel is removed', () => {
  assert.doesNotMatch(source, /const openDetails=node\?\.node_type==='Template' \|\| rows\.length<=6/);
  assert.doesNotMatch(source, /<Collapsible title="Details" count=\{`\$\{rows\.length\} fields`\} open=\{openDetails\}>/);
  assert.doesNotMatch(source, /function reportingMetadataRows\(node\)/);
});

test('reporting graph distinguishes templates instructions and XBRL sources visually', () => {
  assert.match(source, /function reportingVisualKind\(node\)/);
  assert.match(source, /visual==='template'/);
  assert.match(source, /visual==='instruction'/);
  assert.match(source, /visual==='xbrl_source'/);
  assert.match(source, /function isXbrlSourceDocument\(n\)/);
  assert.match(source, /reporting_xbrl_source:'XBRL taxonomy'/);
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
