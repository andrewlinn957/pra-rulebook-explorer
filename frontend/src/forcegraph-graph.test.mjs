import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');
const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));

test('graph view uses ForceGraph2D rather than Cytoscape', () => {
  assert.match(source, /import ForceGraph2D from 'react-force-graph-2d'/);
  assert.match(source, /import \{ forceCollide \} from 'd3-force'/);
  assert.doesNotMatch(source, /import cytoscape from 'cytoscape'/);
  assert.equal(pkg.dependencies['react-force-graph-2d'], '^1.29.1');
  assert.equal(pkg.dependencies['d3-force'], '^3.0.0');
  assert.ok(!pkg.dependencies.cytoscape);
});

test('ForceGraph data preserves parent child document and parallel edge metadata', () => {
  assert.match(source, /function forceGraphData\(graph,selected\)/);
  assert.match(source, /role:relativeNodeRole\(node,selected\?\.id,graph\)/);
  assert.match(source, /badge:documentBadge\(node\)/);
  assert.match(source, /parallelCurveDistance\(parallelCounts,key,parallelIndex\)/);
  assert.match(source, /edgeDirectionLabel\(edge,selected\?\.id\)/);
});

test('ForceGraph renderer draws readable canvas labels without PARENT or CHILD text', () => {
  assert.match(source, /function drawGraphNode\(node,ctx,globalScale,selected,graphDensity\)/);
  assert.match(source, /forceGraphNodeLabel\(node,selected,globalScale,graphDensity\)/);
  assert.doesNotMatch(source, /PARENT\\n/);
  assert.doesNotMatch(source, /CHILD\\n/);
  assert.match(source, /node\.role==='parent'/);
  assert.match(source, /node\.role==='child'/);
});

test('dense ForceGraph views show smaller labels instead of hiding ordinary nodes', () => {
  assert.match(source, /const graphDensity=forceGraphDensity\(data\)/);
  assert.match(source, /nodeCanvasObject=\{\(node,ctx,globalScale\)=>drawGraphNode\(node,ctx,globalScale,selected,graphDensity\)\}/);
  assert.match(source, /function forceGraphDensity\(data\)/);
  assert.match(source, /if\(nodes>=70 \|\| links>=150 \|\| links\/Math\.max\(1,nodes\)>2\.4\) return 'dense'/);
  assert.doesNotMatch(source, /graphDensity==='dense' && !importantNode && globalScale<1\.85/);
  assert.match(source, /const denseSmallLabel=graphDensity==='dense' && !importantNode/);
  assert.match(source, /drawCanvasLabel\(ctx,label,node\.x,node\.y\+radius\+9\/globalScale,denseSmallLabel\?7/);
});

test('ForceGraph uses visible directional arrows and separate parallel links', () => {
  assert.match(source, /linkDirectionalArrowLength=\{e=>e\.edge_type==='contains'\?0:10\.5\}/);
  assert.match(source, /linkDirectionalArrowRelPos=\{e=>e\.edge_type==='contains'\?1:\.72\}/);
  assert.match(source, /linkDirectionalArrowColor=\{e=>edgeDirectionColour\(e,selected\?\.id\)\}/);
  assert.match(source, /linkCanvasObject=\{\(edge,ctx,globalScale\)=>drawGraphLink\(edge,ctx,globalScale,selected\)\}/);
});

test('busy graph nodes receive extra collision spacing', () => {
  assert.match(source, /forceCollide\(node=>forceNodeCollisionRadius\(node\)\)\.strength\(\.88\)/);
  assert.match(source, /function forceNodeCollisionRadius\(node\)/);
  assert.match(source, /const busyBonus=Math\.min\(34,Math\.log2\(Math\.max\(1,node\.degree\|\|1\)\)\*7\)/);
});

test('legend exposes clickable node and edge type filters', () => {
  assert.match(source, /function Legend\(\{active,relationshipTypes,relationshipFilters,availableEdgeTypes,onToggle,onToggleRelationship\}\)/);
  assert.match(source, /onClick=\{\(\)=>onToggle\(t\)\}/);
  assert.match(source, /onClick=\{\(\)=>onToggleRelationship\(t\)\}/);
  assert.match(source, /legend-title">Node types/);
  assert.match(source, /legend-title">Edge types/);
});

test('graph focus uses the selected-node framing when selection changes', () => {
  assert.match(source, /function frameNode\(fg,node,duration=360\)/);
  assert.match(source, /frameNode\(fg,node,420\)/);
  assert.match(source, /function focusNode\(n\)[\s\S]*frameNode\(fg,node\)/);
});

test('graph legend is compact and sits below the graph', () => {
  assert.match(styles, /\.legend\{[^}]*left:50%[^}]*bottom:18px[^}]*transform:translateX\(-50%\)[^}]*grid-template-columns:1fr[^}]*width:min\(340px,calc\(100% - 96px\)\)[^}]*max-height:92px/);
});

test('narrow desktop layout reserves space for the open inspector instead of drawing graph underneath it', () => {
  assert.match(styles, /@media\(max-width:1050px\)\{[\s\S]*?\.shell\.panel-open\{grid-template-columns:280px minmax\(0,1fr\) 390px\}/);
  assert.match(styles, /\.shell\.panel-open \.inspector\.open\{position:static;grid-column:3;grid-row:2/);
});
