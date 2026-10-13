# PRA Rulebook Explorer

PRA Rulebook Explorer is an independent research prototype for exploring UK
prudential material as a local graph.

The repository contains Python ingestion and API code, a React interface, a
SQLite data model, reporting-estate projections and automated tests. It is not
an official Bank of England or Prudential Regulation Authority product.

## What the code does

The code can:

- fetch and parse publicly available PRA Rulebook parts, glossary terms, the
  CRR Terms List, guidance pages, legal instrument listings and selected
  regulatory reporting sources;
- store provisions, definitions, documents, reporting requirements, editions,
  templates, instructions, workbook sheets, cells and relationships in a local
  SQLite-backed model;
- build graph relationships from page structure, hyperlinks and extracted
  references;
- add direct definition and legal-reference relationships, plus roll-ups that
  help readers explore container provisions;
- use language-model review workflows to identify additional relationships
  for human review;
- expose a FastAPI service for health checks, statistics, search, node and edge
  queries, neighbourhoods, paths, graph analysis, validation and feedback;
- provide a React interface for searching and reading provisions, following
  references, viewing graph relationships and reviewing source evidence;
- provide a reporting view that separates supervisory returns from Pillar 3
  disclosures and links reporting requirements to editions, templates,
  instructions, sources and parsed workbook cells where available;
- trace reporting references from a changed provision and separate direct
  instruction evidence from possible downstream scope;
- provide scripts and tests for scraping, enrichment, projection, repair,
  validation and data-quality review.

The [data lifecycle](docs/data-lifecycle.md) and [reporting estate ontology](docs/reporting-estate-ontology.md)
describe the main data and projection models.

Install development dependencies with `pip install -r requirements-dev.txt`.
`scripts/test.sh` runs the fixture-safe Python suite, frontend tests and
production build. `scripts/test-corpus.sh` runs the two additional checks that
need the local reporting database and workbook files.

## What the code does not do

The project does not:

- provide the official PRA Rulebook, an official regulatory database or an
  official interpretation of any rule, guidance or legal instrument;
- provide legal, regulatory or compliance advice;
- guarantee that scraped material is complete, current, correctly parsed or
  effective on a particular date;
- treat inferred or language-model-derived relationships as authoritative
  source evidence;
- decide whether a firm complies with a requirement or make a supervisory
  decision;
- automatically confirm a reporting change, identify a final set of affected
  cells or amend a reporting template;
- submit regulatory returns, change regulatory systems or connect firms to the
  PRA or the Bank of England;
- generate embedding-based similarity scores or semantic-map views;
- expose pairwise shared-term or regex-extracted obligation patterns as graph
  relationships; those retired analysis rows remain in old local databases
  for provenance but are hidden from the serving graph;
- provide a managed public service, user accounts, access control or a
  production availability guarantee;
- include a guaranteed current copy of the scraped corpus in the GitHub
  repository. Raw downloads, local databases, graph exports and other generated
  artefacts are rebuildable local outputs and may be excluded from Git.

Users should check the original source and obtain appropriate expert review
before relying on any result.

## OpenClaw system integration contract

This project is the `pra-rulebook-explorer` workstream. The workspace-wide
agent loop and authority/evidence distinction are defined in
`../../OPENCLAW_SYSTEM.md`; the project-specific documents remain authoritative
for its data model, provenance rules and limitations.

- Canonical project intent: this README and the relevant design documents under
  `docs/`.
- Canonical execution reference: `reference_plan.md` and the current project
  plan for the change being made.
- Evidence: tests, source URLs, ingestion reports, database validation output
  and generated artefacts. Record a meaningful cross-project change in
  `state/openclaw/receipts/`.
- Risk class: research and local-data mutation; inferred or model-derived
  relationships never become authoritative merely because they are indexed.
- Next verification: run the smallest relevant test or validation command and
  inspect the source/provenance path before relying on a result.

Do not treat the workstream or capability indexes as a substitute for the
project's source and provenance checks.

## Source material and provenance

The application records source URLs and, where available, extracted passages,
confidence and the method used to create a relationship. These details help a
reviewer distinguish an explicit source link from a derived or inferred one.

The source material comes from the PRA, the Bank of England and other external
publishers. Those organisations retain their own rights, terms and trademarks.
This repository does not relicense third-party source material.

## Licence

The software in this repository is licensed under the PolyForm Noncommercial
License 1.0.0.

The licence permits non-commercial purposes, subject to its full terms.
Commercial use requires a separate licence from the copyright holder.

Required notice: Copyright 2026 Andrew Linn

Read the [full licence](LICENSE). The licence text is also available from the
[PolyForm website](https://polyformproject.org/licenses/noncommercial/1.0.0/).
