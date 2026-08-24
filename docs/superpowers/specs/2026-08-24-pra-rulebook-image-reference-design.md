# PRA Rulebook image-reference workspace design

## Objective

Recompose the PRA Rulebook graph workspace so it matches the supplied reference image: a compact deep-green navigation rail, a light three-column desktop workbench, a calm command bar, an airy graph canvas, and a readable selected-material inspector.

The change is visual and compositional. Existing graph, search, reporting, reading-mode, filtering, feedback and source-link behaviours remain available.

## Reference observations

- The reference is a 1280px-wide desktop workspace with a 200px navigation rail, a central graph workspace, and an approximately 370px inspector.
- The rail is a full-height dark green surface. It has a small shield mark and collapse control at the top, a compact product heading and breadcrumb, two small actions, a selected navigation row, an uncluttered list of sibling parts, and quiet utility icons at the bottom.
- The top bar starts after the rail. It is white, shallow and lightly bordered. The search control is wide, white and rounded, with a dark-green Search button. Graph and Reporting are compact segmented controls on the right, followed by two small icon controls.
- The graph workspace uses Mist `#F4F7F5` and White surfaces with very light Sage Stone borders. A rounded metadata card sits near the top-left of the canvas. The graph has substantial empty space, small white label pills, deep-green rule nodes, teal child links, pink/red incoming links, dashed teal cross-reference links, a white legend at bottom-left and a vertical zoom control at bottom-right.
- The inspector is a white rounded panel inset from the workspace edges. Its top tabs are compact, with Connections selected by a teal underline. The selected material section uses a small type label, a light chip, a two-line title, an underlined source link, a light text well with a scrollbar, a pale aqua reading-mode call to action and a restrained report button. Cross-references follow below a divider.
- Typography is compact, dark green and mostly sans serif. Labels use uppercase tracking sparingly. Shadows are soft and low contrast. Corners are consistently rounded, generally 8–12px.

## Design

### Shell geometry

Use a desktop grid of `200px minmax(0, 1fr) 370px` with a `58px` top bar. Keep the rail spanning the full viewport height and place the command bar across the central and inspector columns. At widths below 980px, collapse to the existing single-column/mobile behaviour rather than allowing the inspector to compress the graph into an unusable strip.

The graph and inspector should have 16px outer insets on the light workspace. The inspector remains visible by default on desktop so the first view communicates the selected-material workflow shown in the reference.

### Navigation rail

Add a small inline shield mark and a compact collapse affordance to the rail header. Use the existing PRA Rulebook heading and result list, but tighten the hierarchy:

- product heading at 14px, supporting context at 10–11px;
- compact actions with transparent dark-surface treatment;
- selected result row with `#244838` raised surface, a Mineral Aqua marker and white text;
- unselected rows with small white markers and muted Sage Stone text;
- a quiet bottom utility strip for theme/help/exit affordances.

### Command bar

Keep the existing search form and view controls. Restyle it as a single 42px-high white command surface with a 1px Sage Stone border, a 9px radius and a dark-green Search button. Use a pale Mineral Aqua fill for the active Graph or Reporting mode. The panel and settings controls should be icon-sized white buttons with restrained borders.

### Graph canvas

Use a light Mist canvas with no dark gradients. Position the metadata card at 18px from the top and 18px from the left/right edges, with a white surface and a low shadow. Keep force-graph interaction and labels, but ensure:

- rule nodes use deep green or raised green fills and selected nodes have a visible Mineral Aqua outline;
- ordinary child/contains links use Mineral Aqua or a restrained green-blue;
- incoming/reference links use the approved red/pink semantic accents;
- inferred/cross-reference links are dashed;
- labels are white pills with dark-green text and a subtle Sage Stone border;
- legend, navigation help and zoom controls use white cards, not dark overlays;
- the legend stays compact and bottom-left, while zoom controls stay vertically stacked bottom-right.

### Inspector

Make the inspector an inset white surface with a 12px radius and 1px Sage Stone border. Preserve the current `Explore` content and its accessible labels, but reduce visual weight around it. Use a two-tab presentation where the existing third tab, if present, remains functionally available without disrupting the reference layout. The selected material title and text well should fit the visible 370px column without forcing the whole page to scroll.

The reading-mode entry is a pale Aqua card with a small uppercase kicker and a bold one-line action. Links on the light surface are Clear Azure `#23759D` and underlined. The report action is a compact outlined button.

### Reporting and reading modes

Carry the same light workspace language into reporting mode: white toolbar and inspector, Mist background, thin Sage Stone separators, compact active states and dark-green typography. Keep the existing reporting data and controls.

Keep reading mode light, with a white reading header and spine, Mist surrounding background, and a white reference shelf. It should feel like the inspector expanded into a reading surface rather than switching to a separate dark or sepia theme.

### Responsive behaviour

At desktop widths, prioritise the reference proportions. At widths below 980px, retain the current mobile fallback: hide the rail, allow the inspector to overlay or stack, and preserve keyboard focus visibility. At widths below 560px, reduce padding but keep the same surface and colour hierarchy.

## Constraints and non-goals

- Do not change API endpoints, graph filtering semantics, node/edge data, reporting queries, reading-mode data loading or feedback submission.
- Do not use colour as the only signal. Keep the existing labels, arrows, dashed lines, node shapes and legend markers.
- Do not introduce a new component library or a second design-token system. Extend the existing shared colour tokens and CSS.
- Do not restore the rejected full-dark workspace or the previous blue/sepia treatment.

## Acceptance criteria

1. At 1280×853, the graph view has a 200px dark-green rail, a shallow white top bar, a light Mist canvas and a visible right inspector of approximately 370px.
2. The graph view visibly contains the reference composition: rounded metadata card, airy light canvas, deep-green nodes, restrained coloured links, white label pills, compact legend and vertical zoom controls.
3. The selected-material inspector is a white inset panel with compact tabs, readable title/text well, underlined light-surface source link, pale Aqua reading-mode card and cross-reference content below.
4. Reporting and reading mode retain light White/Mist surfaces and the same compact typography and border treatment.
5. Existing frontend tests, new reference-layout regression tests and the production build pass.
6. A browser smoke check at 1280×853 records computed geometry and screenshots for graph, reporting and reader surfaces.
