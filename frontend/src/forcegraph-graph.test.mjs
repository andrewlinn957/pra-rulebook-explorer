import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
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
  assert.match(source, /function drawGraphNode\(node,ctx,globalScale,selected\)/);
  assert.match(source, /forceGraphNodeLabel\(node,selected,globalScale\)/);
  assert.doesNotMatch(source, /PARENT\\n/);
  assert.doesNotMatch(source, /CHILD\\n/);
  assert.match(source, /node\.role==='parent'/);
  assert.match(source, /node\.role==='child'/);
});

test('ForceGraph uses visible directional arrows and separate parallel links', () => {
  assert.match(source, /linkDirectionalArrowLength=\{e=>e\.edge_type==='contains'\?0:10\.5\}/);
  assert.match(source, /linkDirectionalArrowRelPos=\{e=>e\.edge_type==='contains'\?1:\.72\}/);
  assert.match(source, /linkDirectionalArrowColor=\{e=>edgeDirectionColour\(e,selected\?\.id\)\}/);
  assert.match(source, /linkCanvasObject=\{\(edge,ctx,globalScale\)=>drawGraphLink\(edge,ctx,globalScale,selected\)\}/);
});

test('legend exposes clickable node and edge type filters', () => {
  assert.match(source, /function Legend\(\{active,relationshipTypes,relationshipFilters,availableEdgeTypes,onToggle,onToggleRelationship\}\)/);
  assert.match(source, /onClick=\{\(\)=>onToggle\(t\)\}/);
  assert.match(source, /onClick=\{\(\)=>onToggleRelationship\(t\)\}/);
  assert.match(source, /legend-title">Node types/);
  assert.match(source, /legend-title">Edge types/);
});
