# Plan request: get the PRA reporting graph to a fully fixed state

## What I want from you

Please produce a practical, implementation-ready plan for getting the PRA Rulebook Explorer reporting graph to a fully fixed and trustworthy state.

The plan should be suitable for an engineering agent to execute task by task. It should include exact phases, data checks, deterministic rules, tests, verification queries, and acceptance criteria.

Do not suggest exposing audit metadata in the UI. Audit and cleanup metadata may exist in internal tables, but graph nodes and user-facing UI must not show it.

## Project context

Project path:

```text
/root/.openclaw/workspace/projects/pra-rulebook-explorer
```

Database:

```text
backend/data/rulebook.sqlite3
```

The app builds a graph of PRA reporting obligations, returns, templates, source documents, instructions, provisions, concepts, taxonomy artefacts, and external references.

The reporting graph is shown in the frontend reporting tab. The important live endpoint is:

```text
http://127.0.0.1:8100/reporting/graph/overview?selected_return=PRA101&limit=20&child_limit=200&include_datapoints=false
```

A healthy `PRA101` response currently has:

```json
{
  "nodes": 15,
  "edges": 17,
  "node_types": {
    "DataItem": 1,
    "ExternalReference": 1,
    "InstructionSet": 1,
    "PolicyStatement": 2,
    "ReportingObligation": 1,
    "SourceDocument": 6,
    "Template": 2,
    "TemplateSet": 1
  },
  "nodes_with_audit_metadata": 0
}
```

## Recent work already done

A reporting node audit was run over 3,091 nodes. Non-clean findings were imported and processed through deterministic cleanup scripts.

Current audit cleanup result:

```text
reporting_node_cleanup:
implemented: 386
discarded: 863
unresolved: 0
```

Important implemented changes:

- 17 safe `ExternalReference -> LegalInstrument` reclassifications were applied for materialised IFRS/IAS-style references.
- many structural findings were treated as implemented where the graph already represented the relationship correctly, for example:
  - `Template -> USES_INSTRUCTIONS -> InstructionSet`
  - `InstructionSet -> EVIDENCED_BY -> SourceDocument`
  - `TemplateSet -> CONTAINS -> Template`
- one missing instruction provenance edge was repaired:
  - `instruction_set:AnnexXXV -> EVIDENCED_BY -> source_document:5f6fe1051d87ee49`
  - source URL: `corep-liquidity-instructions.pdf`

Important discarded findings:

- attempts to turn provisions, concepts, reporting obligations, templates, template sets, or external references into `InstructionSet` without deterministic evidence
- attempts to turn `Provision` into `LegalInstrument` where the graph model should keep article/provision nodes distinct from legal-instrument/reference nodes
- LLM duplicate-source claims that did not identify deterministic duplicate `source_document` rows

Source document classifier and dedupe were added.

Current source cleanup result:

```text
source_document_cleanup:
canonical: 98,878
duplicate_rewired: 17
edges rewired from duplicate source nodes: 28
edges still pointing to duplicate source nodes: 0
```

Source classifier counts:

```text
taxonomy_xml: 92,605
taxonomy_schema: 5,475
taxonomy_package: 361
instruction_pdf: 176
template_workbook: 114
policy_html: 50
pdf_document: 47
policy_pdf: 26
template_pdf: 25
webpage_html: 9
rulebook_html: 7
```

The dedupe logic is deliberately conservative:

- exact normalised HTTP(S) URL dedupe for human-facing pages/documents
- no checksum-only dedupe across taxonomy XML/XSD artefacts
- unpacked taxonomy artefacts that inherit the same parent ZIP URL must not be collapsed
- duplicate graph source nodes are removed only after all graph edges are rewired
- underlying `source_document` rows are kept for auditability

## Important files

Backend reporting graph code:

```text
backend/app/reporting.py
```

Frontend reporting UI:

```text
frontend/src/main.jsx
frontend/src/quality-dashboard.test.mjs
```

General reporting package builder:

```text
scripts/build_reporting_graph_packages.py
```

COR011-specific semantic loader:

```text
scripts/semantic_reporting_extraction.py
```

The COR011 loader is a bespoke loader for one liquidity return, not the general model. It seeds COR011-specific nodes and edges such as `template_set:AnnexXXIV` and `instruction_set:AnnexXXV`. A complete plan should decide whether to generalise this pattern across all returns or retire it once the general package builder produces equivalent evidence.

Audit and cleanup scripts:

```text
scripts/audit_reporting_nodes.py
scripts/cleanup_reporting_node_audit.py
scripts/source_document_cleanup.py
scripts/enrich_reporting_templates.py
```

Tests added or modified:

```text
tests/test_reporting_node_audit_cleanup.py
tests/test_source_document_cleanup.py
tests/test_reporting_graph.py
tests/test_reporting_template_enrichment.py
frontend/src/quality-dashboard.test.mjs
```

## Current verification commands

These passed after the latest cleanup work:

```bash
.venv/bin/python -m unittest \
  tests.test_source_document_cleanup \
  tests.test_reporting_node_audit_cleanup \
  tests.test_reporting_graph \
  tests.test_reporting_template_enrichment -v

node --test frontend/src/*.test.mjs

npm --prefix frontend run build
```

Key DB checks currently pass:

```sql
select count(*) as unresolved_audit
from reporting_node_cleanup
where decision not in ('implemented','discarded');

select count(*) as graph_nodes_with_audit_cleanup
from graph_node
where json_type(properties_json,'$.audit_cleanup') is not null;

select count(*) as edges_to_duplicate_sources
from graph_edge e
join source_document_cleanup c
  on c.decision='duplicate_rewired'
 and (
   e.source_node_id='source_document:'||c.source_id
   or e.target_node_id='source_document:'||c.source_id
 );
```

Expected result for all 3 is `0`.

## Core problem to solve

The reporting graph is much better, but it is not yet guaranteed to be fully correct.

The main remaining risk is that the graph combines several concepts that need a disciplined model:

- physical source files and pages, represented by `SourceDocument`
- semantic instruction sets, represented by `InstructionSet`
- reporting returns/data items, represented by `DataItem` and `ReportingObligation`
- templates and template sets, represented by `Template` and `TemplateSet`
- legal provisions, represented by `Provision`
- laws, regulations, accounting standards and other external legal/reference material, represented by `LegalInstrument` or `ExternalReference`
- policy statements and supervisory material, represented by `PolicyStatement`
- taxonomy package artefacts, XML, XSD, XBRL, ZIP files and sample instances

The previous LLM audit often spotted useful symptoms but recommended the wrong operation. For example, it suggested changing a `Template` or `SourceDocument` to `InstructionSet`, when the correct graph fix was usually to add or verify an edge such as:

```text
Template -> USES_INSTRUCTIONS -> InstructionSet
InstructionSet -> EVIDENCED_BY -> SourceDocument
DataItem -> USES_TEMPLATE -> Template or TemplateSet
TemplateSet -> CONTAINS -> Template
DataItem -> EVIDENCED_BY -> SourceDocument
```

## What the plan should cover

Please produce a plan that gets the reporting graph to a fully fixed state. Include at least the following.

### 1. Define the target graph model

Specify the intended semantics for each node type and edge type used in the reporting graph.

At minimum, cover:

- `DataItem`
- `ReportingObligation`
- `Template`
- `TemplateSet`
- `InstructionSet`
- `SourceDocument`
- `Provision`
- `LegalInstrument`
- `ExternalReference`
- `PolicyStatement`
- `Concept`
- taxonomy artefacts

Explain which things must remain distinct. For example, a PDF instruction file should normally remain a `SourceDocument`, while the semantic instruction object should be an `InstructionSet` linked to that file.

### 2. Define graph invariants

Propose SQL checks that should always pass. Include checks for:

- every `DataItem` has the expected `ReportingObligation` relationship where applicable
- every current return has at least one source document or taxonomy evidence source
- every `Template` belongs to a `TemplateSet` where the source estate supports it
- every `Template` that uses instructions has a valid `InstructionSet`
- every `InstructionSet` has at least one `EVIDENCED_BY` source document where source evidence exists
- source documents are not duplicated in graph edges after canonical dedupe
- taxonomy child files are not collapsed into their parent package source
- provisions are not incorrectly retyped as legal instruments
- policy statements are not incorrectly retyped as legal instruments
- graph nodes do not contain audit metadata in `properties_json`

### 3. Audit the remaining discarded findings

Use the `reporting_node_cleanup` table as evidence, but do not assume its decisions are final.

For each discarded family, decide whether there is useful information to convert into deterministic checks or fixes.

Important families include:

```text
Provision -> InstructionSet: 248
Provision -> LegalInstrument: 107
SourceDocument -> InstructionSet: 83 discarded, 75 implemented
ReportingObligation -> InstructionSet: 63
ExternalReference -> InstructionSet: 50
TemplateSet -> InstructionSet: 38
LegalInstrument -> LegalInstrument category mismatch: 53
Template -> TemplateSet: 19 discarded, 38 implemented
Template -> ReportingObligation: 17
Provision -> ReportingObligation: 15
Concept -> InstructionSet: 13
PolicyStatement -> InstructionSet: 10
```

For each family, classify it as one of:

- valid and implementable now
- valid symptom, but needs a different graph operation
- invalid because it conflicts with the target graph model
- ambiguous and needs source inspection

For ambiguous cases, say exactly which source PDFs, workbooks, taxonomy files or DB rows should be inspected and what evidence would decide the case.

### 4. Generalise or replace the COR011 semantic loader

The current `scripts/semantic_reporting_extraction.py` is COR011-specific. It creates useful semantic edges for one return, but it is not a general solution.

Plan how to handle it.

Options to assess:

- keep COR011 loader as a special source until the general builder can replace it
- generalise the same pattern for all returns using deterministic package evidence
- retire the COR011 loader and move all equivalent logic into `scripts/build_reporting_graph_packages.py`

Recommend one approach.

The plan should include how to prevent one-off COR011 logic from becoming a hidden inconsistency in the wider graph.

### 5. Improve source document classification

The current deterministic classifier uses file type, URL, title and path. It does not yet inspect PDF text except where manually checked.

Plan whether and how to add deeper classification, for example:

- first-page PDF text for instruction/template/policy classification
- workbook sheet names for template workbook classification
- taxonomy manifest/package metadata for taxonomy package classification
- source URL lineage for child artefacts extracted from ZIPs

Include safeguards to avoid classifying every taxonomy child file as the same source just because it inherited a parent URL.

### 6. Improve source deduplication

The current dedupe is deliberately conservative. It found and rewired 17 duplicate HTML/publication page variants.

Plan further dedupe stages, including:

- exact normalised URL dedupe
- checksum dedupe for human-facing documents only
- title plus URL lineage dedupe for Bank/PRA pages
- explicit non-dedupe rules for taxonomy package children
- graph edge rewiring
- orphan duplicate graph node removal
- audit trail preservation

Specify which dedupe rules should never be used.

### 7. Source-backed validation against PDFs and Excel workbooks

The plan should include a method for checking the source documents, not just graph metadata.

Available local files are under paths like:

```text
backend/data/raw/reporting-sources/**/files/
```

There are PDFs, XLSX/XLSM/XLTX/XLS, XML, XSD, XBRL, ZIP and HTML-derived sources.

The plan should say how to inspect:

- PDFs containing instructions
- PDFs containing templates
- Excel workbooks containing templates
- taxonomy packages and their child files
- PRA Rulebook HTML pages
- Bank of England publication pages

Include exact recommended Python libraries or command-line tools where useful. Current project venv already has `pypdf`; system `pdftotext` was not available.

### 8. Build a final reporting graph quality gate

Propose a single repeatable command or script that runs all relevant checks and exits non-zero if the reporting graph is not fixed.

It should combine:

- DB invariant checks
- API smoke tests for selected returns
- frontend build or test checks if graph data shape changes
- source classifier/dedupe consistency checks
- audit cleanup resolution checks

The result should be usable in CI or by an agent before claiming the graph is fixed.

### 9. Acceptance criteria for “totally fixed”

Define measurable acceptance criteria. Avoid vague terms.

Include criteria such as:

- no unresolved audit recommendations
- all discarded recommendations have a precise reason tied to graph model semantics or source evidence
- all valid source-document findings are implemented as source classification, provenance edges, or dedupe rewiring
- no audit metadata is exposed in graph API nodes or frontend UI
- no graph edges point to duplicate source nodes
- representative returns load correctly through the live API
- graph invariants pass for all reporting returns, not only PRA101 or COR011
- source classification has deterministic coverage for all source documents

## Output format requested

Please write the plan as a structured implementation plan with these sections:

1. executive summary
2. target graph model
3. current gaps and risks
4. implementation phases
5. task-by-task plan with files to change
6. SQL invariants and expected results
7. source inspection approach
8. test and verification strategy
9. final acceptance criteria
10. risks and rollback strategy

For each implementation phase, include:

- goal
- files to create or modify
- deterministic rules to implement
- tests to add
- commands to run
- expected outputs

Please be concrete. The plan should be something an engineering agent can execute without needing more context.
