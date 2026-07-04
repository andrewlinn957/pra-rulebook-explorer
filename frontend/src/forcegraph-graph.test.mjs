import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');
const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));

test('graph view uses ForceGraph2D rather than Cytoscape', () => {
  assert.match(source, /import ForceGraph2D from 'react-force-graph-2d'/);
  assert.match(source, /import \{ forceCollide, forceX, forceY \} from 'd3-force'/);
  assert.doesNotMatch(source, /import cytoscape from 'cytoscape'/);
  assert.equal(pkg.dependencies['react-force-graph-2d'], '^1.29.1');
  assert.equal(pkg.dependencies['d3-force'], '^3.0.0');
  assert.ok(!pkg.dependencies.cytoscape);
});

test('ForceGraph data preserves parent child document and collapsed parallel edge metadata', () => {
  assert.match(source, /function forceGraphData\(graph,selected\)/);
  assert.match(source, /const role=relativeNodeRole\(node,selected\?\.id,graph\)/);
  assert.match(source, /role,layoutLane:forceNodeLayoutLane/);
  assert.match(source, /badge:documentBadge\(node\)/);
  assert.match(source, /collapseParallelEdges\(visibleEdges\)\.map/);
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

test('ForceGraph uses visible directional arrows and collapsed parallel links', () => {
  assert.match(source, /linkDirectionalArrowLength=\{e=>e\.edge_type==='contains'\?0:10\.5\}/);
  assert.match(source, /linkDirectionalArrowRelPos=\{e=>e\.edge_type==='contains'\?1:\.72\}/);
  assert.match(source, /linkDirectionalArrowColor=\{e=>edgeDirectionColour\(e,selected\?\.id\)\}/);
  assert.match(source, /linkCanvasObject=\{\(edge,ctx,globalScale\)=>drawGraphLink\(edge,ctx,globalScale,selected\)\}/);
  assert.match(source, /function collapseParallelEdges\(edges\)/);
});

test('busy graph nodes receive extra collision spacing', () => {
  assert.match(source, /forceCollide\(node=>forceNodeCollisionRadius\(node\)\)\.strength\(\.88\)/);
  assert.match(source, /function forceNodeCollisionRadius\(node\)/);
  assert.match(source, /const busyBonus=Math\.min\(34,Math\.log2\(Math\.max\(1,node\.degree\|\|1\)\)\*7\)/);
});

test('selected-node graph layout biases relationship groups into compass lanes', () => {
  assert.match(source, /import \{ forceCollide, forceX, forceY \} from 'd3-force'/);
  assert.match(source, /layoutLane:forceNodeLayoutLane\(\{\.\.\.node,role\},selected\?\.id,graph\.edges\|\|\[\]\)/);
  assert.match(source, /fg\.d3Force\('x',forceX\(node=>forceNodeTargetX\(node\)\)\.strength\(node=>forceNodeAxisStrength\(node\)\)\)/);
  assert.match(source, /fg\.d3Force\('y',forceY\(node=>forceNodeTargetY\(node\)\)\.strength\(node=>forceNodeAxisStrength\(node\)\)\)/);
  assert.match(source, /if\(\['defined_term','glossary','crr_terms_list'\]\.includes\(node\.node_type\)\) return 'northEast'/);
  assert.match(source, /if\(node\.role==='parent'\) return 'north'/);
  assert.match(source, /if\(node\.role==='child'\) return 'south'/);
  assert.match(source, /if\(node\.layoutLane==='northEast'\) return 260/);
  assert.match(source, /if\(node\.layoutLane==='northEast'\) return -220/);
  assert.match(source, /if\(edge\.edge_type==='references' && edge\.to_node_id===selectedId\) return 'west'/);
  assert.match(source, /if\(edge\.edge_type==='references' && edge\.from_node_id===selectedId\) return 'east'/);
  assert.match(source, /if\(edge\.from_node_id===selectedId && isPurpleAnalysisNode\(node\)\) return 'east'/);
  assert.match(source, /function isPurpleAnalysisNode\(node\)/);
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
  assert.match(source, /fg\.zoom\(1\.35,duration\)/);
  assert.match(source, /frameNode\(fg,node,420\)/);
  assert.match(source, /function focusNode\(n\)[\s\S]*frameNode\(fg,node\)/);
});

test('graph canvas measures its visible column so inspector space is excluded from centring', () => {
  assert.match(source, /const \[graphSize,setGraphSize\]=useState\(\{width:0,height:0\}\)/);
  assert.match(source, /new ResizeObserver\(\(\[entry\]\)=>/);
  assert.match(source, /width=\{graphSize\.width\}/);
  assert.match(source, /height=\{graphSize\.height\}/);
});

test('graph legend is compact and sits at the bottom left of the graph', () => {
  assert.match(styles, /\.legend\{[^}]*left:18px[^}]*bottom:18px[^}]*top:auto[^}]*transform:none[^}]*grid-template-columns:repeat\(2,max-content\)[^}]*gap:2px 8px[^}]*width:max-content[^}]*max-width:calc\(100% - 96px\)[^}]*max-height:132px/);
  assert.match(styles, /\.legend button,\.legend div\{[^}]*gap:3px[^}]*font-size:8\.5px/);
  assert.match(styles, /\.legend button em\{[^}]*margin-left:3px/);
});

test('parallel edges between the same two nodes collapse into one link with a count badge', () => {
  assert.match(source, /function collapseParallelEdges\(edges\)/);
  assert.match(source, /parallelCount/);
  assert.match(source, /drawParallelEdgeCount\(edge,ctx,globalScale\)/);
  assert.match(source, /if\(edge\.parallelCount>1\) drawParallelEdgeCount/);
});

test('narrow desktop layout reserves space for the open inspector instead of drawing graph underneath it', () => {
  assert.match(styles, /@media\(max-width:1050px\)\{[\s\S]*?\.shell\.panel-open\{grid-template-columns:280px minmax\(0,1fr\) 390px\}/);
  assert.match(styles, /\.shell\.panel-open \.inspector\.open\{position:static;grid-column:3;grid-row:2/);
});
