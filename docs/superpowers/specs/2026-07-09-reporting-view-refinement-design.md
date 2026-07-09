# Reporting view refinement design

## Scope

Refine the PRA Rulebook Explorer reporting view without mockups. This pass covers two user-approved improvements:

1. Improve the returns overview so reporting returns are easier to scan, compare and enter.
2. Improve the return drilldown and inspector so selected templates, sources, rules and datapoint summaries are more useful.

Out of scope for this pass: changing backend extraction logic, adding new source ingestion, or redesigning the main Rulebook graph.

## Approach

Use the existing React reporting view and backend `/reporting/graph/overview` endpoint. Keep the force graph, left rail and right inspector layout, but refine the information architecture and labels rather than introducing a new UI pattern.

## Components

### Returns overview

- Keep grouped return estates, such as COREP, PRA, FSA and FINREP.
- Make each return row more informative, using available metadata where present, for example template count, source document count, submission system or reporting domain.
- Preserve quick drilldown on click.
- Keep search/filter behaviour unchanged.

### Return drilldown rail

- Keep the selected return as the navigation anchor.
- Split related nodes into clearer groups where possible: templates, instructions, sources, rules/legal basis, concepts/scope and datapoints.
- Show concise counts and labels so the rail is not just a flat list.
- Preserve the existing back-to-overview and back-to-return controls.

### Inspector

- Prioritise useful source links, metadata and connected evidence.
- Make metadata labels clearer for reporting-specific fields.
- Reduce noise from technical fields unless they are the only available evidence.
- Keep feedback affordance and graph conventions intact.

## Data flow

- `ReportingGraphView` fetches the graph as today.
- Filtering remains client-side using the existing `filterGraph` path.
- New grouping and display helpers derive sections from the current graph nodes and edges, without additional network calls.

## Error handling

- Preserve the current error banner for failed graph loads.
- Missing metadata should degrade gracefully to the existing title and material type.
- Empty groups should not render.

## Testing

Add or update lightweight frontend tests that inspect source/style conventions, matching the current test pattern in `quality-dashboard.test.mjs` and related presentation tests. Cover:

- Overview rows expose useful reporting metadata.
- Drilldown rail groups related artefacts contextually.
- Inspector continues to show useful links and metadata.
- Existing graph navigation conventions remain unchanged.
