# PRA Rulebook Explorer Spec

**Working name:** PRA Rulebook Explorer  
**Owner:** Andrew  
**Created:** 2026-06-01  
**Purpose:** Build an exploratory graph interface over the PRA Rulebook so users can navigate rules, guidance, definitions, instruments and inferred relationships, and discover connections that are hard to see in the native rulebook.

## Current build status

**Status as at 2026-06-01:** Phase 2 graph backend is now working on the full-corpus prototype.

Completed:

- Project skeleton, local Python scraper package, README and runnable scrape script.
- SQLite node/edge/source store.
- Raw HTML cache and graph JSON export.
- Full PRA Rules crawl from `/pra-rules`: 167 Parts, 6,123 rules, 1,340 chapters.
- Glossary scrape: 766 glossary terms.
- CRR Terms List scrape: 152 CRR terms, using `01-01-2027` because the live page states the list is not effective until then.
- Guidance scrape: 167 SS/SoP guidance documents, 5,003 guidance paragraphs, 566 guidance sections.
- Initial Legal Instruments listing scrape: visible listing page only, currently 6 instruments and 78 `amends` edges.
- Explicit structural and link edges: hierarchy, hyperlinks, glossary/defined-term links, legal-instrument listing links.
- Deterministic derived edges: low/medium-degree shared defined-term links and title-based resolution of dated Part references.
- Phase 1 verification gate: graph had 17,611 nodes, 84,941 edges, zero missing edge targets, and exported to `backend/data/processed/graph.json`.
- Phase 2 FastAPI backend under `backend/app/` with SQLite graph access, FTS search, NetworkX shortest path support, neighbourhood queries, centrality, and interesting connection endpoints.
- Embedding/index pipeline: SQLite FTS5 index, local MiniLM sentence-transformer embeddings for 10,903 text nodes, and 32,625 semantic `similar_to` edges with `source_method=embedding`.
- Latest verification gate: API smoke test passes against full corpus with 25,158 nodes, 145,002 edges, 32,625 MiniLM `similar_to` edges, 983 cross-Part named rule-reference edges, 12 topic nodes, 7,535 obligation-pattern nodes, and zero missing edge targets.

Known gaps:

- Legal Instruments pagination/filter endpoint still needs reverse-engineering to capture all historic instruments.
- Explicit rule-reference regex extraction is not yet separate from HTML hyperlinks.
- Embeddings now use local MiniLM (`sentence-transformers/all-MiniLM-L6-v2`) via the shared sentence-transformers venv; TF-IDF/SVD remains available as a fallback.
- Phase 3 React UI prototype now exists under `frontend/`, with search, neighbourhood graph, filters, node details, evidence/provenance display, interesting connections and central nodes.
- NetworkX analysis now includes shortest path, degree centrality, sampled betweenness centrality, connected components, greedy-modularity community detection and common-neighbour analysis endpoints.

**Current next step:** Phase 4 is underway: regex citation extraction, richer graph analysis, MiniLM embeddings, topic clustering, obligation-pattern extraction and UI refinement.

## 1. Product vision

The app should turn the PRA Rulebook from a hierarchical document site into a navigable regulatory knowledge graph.

Users should be able to:

- browse the Rulebook as a graph of legal/regulatory nodes;
- click a rule and see its neighbouring rules, defined terms, cross-references, topics, firm-scope categories, instruments and guidance;
- search semantically, not just by keyword;
- identify clusters of rules that are connected by common terms, obligations, policy topics or references;
- surface “unexpected” links, such as rules in different parts that share definitions, similar obligations, common permissions, common waivers/modifications, or dependencies on the same CRR/FSMA concepts;
- preserve source provenance so every graph edge can be traced back to rule text, metadata, or an inference method.

The core design principle: **exploration first, explanation always available**. The graph can suggest connections, but the app must make clear why each connection exists.

## 2. Source scope

Primary public source: `https://www.prarulebook.co.uk/`.

Initial site sections to model:

- PRA Rules
  - CRR Firms
  - Non-CRR Firms
  - SII Firms
  - Non-SII Firms
  - Non-authorised Persons
  - Forms
- Glossary
- CRR Terms List
- Legal Instruments
- Guidance
  - Supervisory Statements
  - Statements of Policy
- What’s New / amendment history, if technically harvestable

Important distinction:

- **Rules** contain binding requirements.
- **Supervisory Statements** are expectations/guidance, not absolute requirements.
- **Statements of Policy** explain the PRA’s policy approach.

The app should encode this distinction visibly and in the data model.

## 3. User goals

### Primary users

- Policy/regulatory specialist exploring structure and relationships.
- Supervisor or analyst trying to understand dependencies around a rule or topic.
- Builder/researcher creating a richer PRA knowledge base.

### Key jobs to be done

1. “Show me everything connected to this rule.”
2. “What definitions does this rule rely on?”
3. “Which other parts use the same defined terms?”
4. “Which rules and guidance documents touch this concept?”
5. “Where does this rule sit in the wider prudential framework?”
6. “What links are explicit cross-references versus inferred thematic similarity?”
7. “What changed over time?”

## 4. Core concepts

### Node types

Minimum useful node taxonomy:

| Node type | Description | Example attributes |
|---|---|---|
| `rule` | A numbered rule provision | part, chapter, rule number, text, effective date, firm type, status |
| `part` | Rulebook part/module | title, firm category, URL |
| `chapter` | Chapter or section grouping | title, parent part, URL |
| `guidance_document` | SS or SoP | document type, reference, title, publication date |
| `guidance_section` | Section/paragraph inside guidance | parent document, paragraph number, text |
| `defined_term` | Glossary or CRR term | term, source, definition, URL |
| `legal_instrument` | PRA instrument making/amending rules | instrument number, date, affected parts |
| `external_source` | FSMA, CRR, BOE page, FCA Handbook etc | title, jurisdiction, URL |
| `topic` | Derived conceptual theme | label, description, confidence |
| `obligation_pattern` | Derived obligation/action pattern | verb, object, subject, modality |

### Edge types

Minimum useful edge taxonomy:

| Edge type | Meaning | Source/provenance |
|---|---|---|
| `contains` | Part contains chapter, chapter contains rule | site structure |
| `references` | Text explicitly cites another rule/document | parsed citation |
| `defines` | Glossary/terms list defines a term | glossary source |
| `uses_defined_term` | Rule uses a defined term | exact text match with disambiguation |
| `amends` | Instrument amends rule/part | legal instrument metadata/text |
| `applies_to` | Rule applies to firm type/scope | rulebook section/category/parsed text |
| `has_topic` | Rule/document belongs to topic | classifier/embedding/topic model |
| `similar_to` | Semantic similarity between text units | embeddings, thresholded |
| `shares_term_with` | Two rules use significant common terms | derived from term usage |
| `shares_obligation_pattern` | Similar action/modality pattern | NLP extraction |
| `explained_by` | Guidance explains or is relevant to a rule/topic | explicit reference or inferred relation |
| `commenced_by` | Rule linked to commencement/effective instrument | instrument metadata |

Each edge must store:

- `edge_type`
- `source_method`: `site_structure`, `regex_reference`, `term_match`, `embedding`, `llm_extraction`, `manual`, etc.
- `confidence`: 0-1
- `evidence_text`: short quoted text where available
- `source_url`
- `created_at`
- `version_id`

## 5. Data model

Use a graph-first model, with a relational/search sidecar for retrieval.

Recommended stack for local/MVP:

- **Ingestion/storage:** SQLite for raw pages, parsed nodes, metadata, and extraction logs.
- **Graph:** NetworkX for build-time analysis and export, or Neo4j if we want persistent graph queries early.
- **Search:** SQLite FTS5 for keyword search.
- **Semantic search:** sentence-transformer embeddings stored in SQLite/FAISS/Chroma.
- **Frontend:** React + TypeScript with Cytoscape.js or Sigma.js for graph rendering.
- **API:** FastAPI or Node/Express. FastAPI is probably cleaner for NLP/graph analysis.

Recommended eventual stack:

- Postgres for canonical content/metadata.
- Neo4j or KuzuDB for graph queries.
- pgvector or dedicated vector DB for semantic search.
- FastAPI backend.
- React frontend.
- Scheduled ingestion/versioning pipeline.

Canonical entities:

```text
DocumentSource
  id, source_type, url, fetched_at, content_hash, raw_html, raw_text

Node
  id, node_type, stable_key, title, text, url, source_id, metadata_json, version_id

Edge
  id, from_node_id, to_node_id, edge_type, source_method, confidence,
  evidence_text, source_url, metadata_json, version_id

Embedding
  node_id, model_name, vector, text_hash

ExtractionRun
  id, run_type, started_at, completed_at, status, params_json, errors_json
```

Stable keys matter. A rule should keep the same stable key across ingestion runs if its identity is unchanged, even if the text changes.

## 6. Ingestion pipeline

### Stage 1: Crawl and cache

- Start from known Rulebook section URLs.
- Respect robots/crawl politeness.
- Save raw HTML and extracted text.
- Capture fetch timestamp and hash.
- Do not rely only on live pages at runtime.

### Stage 2: Parse structure

- Identify parts, chapters, rules, paragraphs, tables, definitions and links.
- Preserve original ordering.
- Store canonical URL and breadcrumbs.
- Split long documents into meaningful nodes, not arbitrary chunks.

### Stage 3: Extract explicit links

- Native site links.
- Rule references, e.g. `2.1`, `Chapter 3`, named Rulebook parts.
- Glossary terms and CRR terms.
- Guidance references, SS/SoP references.
- Legal instruments and amendment references.
- External references, e.g. FSMA, CRR, other legislation.

### Stage 4: Enrich with inferred links

- Term co-occurrence.
- Semantic similarity using embeddings.
- Topic modelling or clustering.
- Obligation extraction: modal verbs, subject, action, object, condition.
- Possible guidance-to-rule mapping.

### Stage 5: Quality checks

- Count nodes/edges by type.
- Identify orphan nodes.
- Sample edge evidence.
- Check broken URLs.
- Compare crawl coverage against site index/navigation.
- Flag uncertain/high-impact inferred edges for review.

## 7. Graph analysis features

MVP graph metrics:

- degree centrality: highly connected rules/terms;
- betweenness centrality: bridge provisions/concepts;
- connected components and communities;
- clusters by topic/part/firm type;
- shortest path between two rules/concepts;
- common neighbours between two nodes.

Later features:

- temporal graph diff between Rulebook versions;
- “unexpected connection” ranking using cross-part semantic similarity;
- dependency map for a rule, separating legal references, definitions, guidance and inferred topics;
- impact analysis: if a definition/rule changes, which nodes may be affected;
- topic-specific subgraphs, e.g. capital, liquidity, governance, outsourcing, remuneration, operational resilience.

## 8. Frontend design

### Main layout

- Left panel: search, filters, saved views.
- Centre: interactive graph.
- Right panel: selected node details and evidence.
- Bottom or collapsible panel: edge list, path results, extraction provenance.

### Graph controls

Filters:

- node type;
- edge type;
- source method;
- confidence threshold;
- firm category;
- Rulebook part;
- guidance/rules/instruments;
- explicit-only versus inferred;
- date/version.

Views:

- Rulebook hierarchy view.
- Neighbourhood view for selected node.
- Topic cluster view.
- Definition dependency view.
- Guidance-to-rule view.
- Shortest path view.

Visual encoding:

- Shape = node type.
- Colour = source category or topic, not both at once.
- Edge style = explicit/inferred/manual.
- Edge opacity/width = confidence or weight.
- Labels appear progressively to avoid clutter.

Design principle: avoid a hairball. Start from a selected node or filtered subgraph, not the entire Rulebook by default.

## 9. “Unexpected connections” logic

A connection is potentially interesting where:

- two rules are in different parts but share rare defined terms;
- rules have high semantic similarity but no explicit cross-reference;
- a guidance document appears semantically close to rules it does not cite;
- a defined term is unexpectedly central across unrelated parts;
- a rule bridges two otherwise separate topic communities;
- a legal instrument amended several areas that do not appear adjacent in the hierarchy;
- similar obligation patterns appear under different firm categories.

Ranking factors:

```text
interestingness =
  cross_part_bonus
  + semantic_similarity
  + rare_term_overlap
  + bridge_score
  + guidance_relevance
  - obvious_hierarchy_penalty
  - same_chapter_penalty
```

Every suggested connection must display “why shown”: shared terms, semantic score, common neighbours, evidence snippets, and whether the relation is inferred.

## 10. MVP

### MVP objective

Create a local, working prototype that ingests a subset of the PRA Rulebook and lets Andrew explore rules, terms and explicit/inferred links in a graph.

### MVP scope

Use one initial domain slice, preferably one of:

1. **CRR Firms only**: broad and familiar, good for capital/liquidity/governance discovery.
2. **One topic slice**, e.g. Operational Resilience, Outsourcing, Liquidity, Governance.
3. **One Rulebook part plus Glossary**: easiest to validate extraction quality.

Recommendation: start with **one Rulebook part plus Glossary**, then expand to CRR Firms once parsing is reliable.

### MVP features

Must have:

- crawler/cache for selected Rulebook URLs;
- parser for parts, chapters, rules and glossary terms;
- node/edge store in SQLite;
- explicit edges from:
  - hierarchy;
  - hyperlinks;
  - rule reference regexes;
  - glossary term usage;
- embeddings for rule text and definitions;
- semantic `similar_to` edges above configurable threshold;
- simple graph API;
- React graph UI with:
  - search;
  - selected node neighbourhood;
  - filters for node/edge type;
  - right-side evidence panel;
  - explicit/inferred toggle;
- export to GraphML/JSON for external analysis.

Should have:

- centrality ranking page;
- shortest path between two nodes;
- basic community detection;
- “unexpected connections” list;
- ingestion quality report.

Not in MVP:

- full version history;
- full legal instrument parsing;
- authentication/multi-user support;
- manual annotation workflows;
- production deployment;
- LLM extraction at scale;
- firm-specific compliance workflows.

### MVP acceptance criteria

- Can ingest at least one complete Rulebook part and the Glossary.
- Can search for a rule or defined term and open a graph neighbourhood.
- Every displayed edge has a type and provenance.
- Can distinguish explicit from inferred links.
- Can show at least five non-trivial inferred connections with evidence.
- Can export graph data.
- Parser failures are logged and visible.

## 11. Suggested implementation phases

### Phase 0: Spike — complete

- [x] Confirm page structure and URL patterns.
- [x] Download a handful of pages.
- [x] Build parser proof of concept.
- [x] Produce first node/edge JSON.

### Phase 1: Data foundation — substantially complete

- [x] SQLite schema.
- [x] Crawler/cache.
- [x] Rule/glossary parser.
- [x] CRR Terms List parser.
- [x] Guidance index/detail parser.
- [x] Initial Legal Instruments listing parser.
- [x] Reference extractor for HTML links and defined-term links.
- [x] Graph JSON export and stats output.
- [ ] Dedicated quality report CLI with parser failure detail.
- [ ] Full Legal Instruments pagination/history scrape.
- [x] Separate regex extraction for unlinked same-Part rule references.

### Phase 2: Graph backend — complete for MVP

- [x] Build graph access from SQLite.
- [x] Add NetworkX-backed shortest path plus degree centrality analysis.
- [x] Add FTS5 keyword search index.
- [x] Add semantic embeddings pipeline.
- [x] Add semantic `similar_to` edges.
- [x] API endpoints:
  - [x] `/health`
  - [x] `/search`
  - [x] `/node/{id}`
  - [x] `/node/{id}/neighbourhood`
  - [x] `/path?from_id=&to_id=`
  - [x] `/interesting`
  - [x] `/centrality`
  - [x] `/stats`

### Phase 3: UI prototype — complete for MVP

- [x] React/Vite app under `frontend/`.
- [x] SVG neighbourhood graph view with progressive labels and explicit/inferred edge styling.
- [x] Search and filter panels.
- [x] Node detail/evidence panel.
- [x] Interesting connections view.
- [x] Central nodes view.
- [x] Local run script: `./scripts/run_frontend.sh`.

### Phase 4: Expand coverage — underway

- [x] Add more Rulebook sections.
- [x] Add guidance documents.
- [x] Improve citation extraction with same-Part regex rule references.
- [x] Add initial keyword topic nodes and `has_topic` edges.
- [x] Add initial regex obligation-pattern nodes and `has_obligation_pattern` edges.
- [x] Add richer NetworkX analysis endpoints: betweenness, components, communities and common neighbours.
- [x] Add MiniLM embedding pathway for higher-quality semantic edges.
- [ ] Full Legal Instruments pagination/history scrape.
- [x] Cross-Part named rule-reference extraction for patterns such as `Notifications 10.3` and `Investments 5.2`.
- [ ] Human-reviewed topic taxonomy and obligation normalisation.

### Phase 5: Production hardening

- Versioning.
- Scheduled refreshes.
- Review/annotation workflow.
- Tests and regression checks.
- Deployment.

## 12. Technical choices to decide later

Open questions:

- Graph store: NetworkX-only, KuzuDB, or Neo4j?
- Frontend graph renderer: Cytoscape.js versus Sigma.js/Graphology?
- Embedding model: local MiniLM for speed versus larger legal/domain model for quality?
- Inference approach: deterministic NLP first, LLM extraction later?
- How much historical versioning is available from public Rulebook pages?
- Whether to ingest PDFs or only HTML initially.

Initial recommendation:

- Use **Python + FastAPI + SQLite + NetworkX + local sentence-transformers + React + Cytoscape.js**.
- Keep everything local and inspectable until the extraction logic is reliable.
- Add Neo4j/KuzuDB only when graph queries outgrow NetworkX/SQLite.

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Site structure is inconsistent | Cache raw pages; write parser tests against fixtures |
| Graph becomes unreadable hairball | Default to neighbourhood/subgraph views and filters |
| Inferred edges create false confidence | Label provenance and confidence clearly; evidence panel mandatory |
| Defined-term matching creates false positives | Use case/phrase matching plus glossary-aware disambiguation |
| Rule identity changes across updates | Use stable keys plus content hashes/version IDs |
| Legal nuance lost in embeddings | Treat embeddings as discovery aids, not authoritative mapping |
| Crawl coverage incomplete | Compare against navigation/index pages and report gaps |

## 14. First build task list

1. [x] Create project skeleton.
2. [x] Fetch and cache PRA Rulebook landing page plus one target part and Glossary.
3. [x] Inspect HTML structure and identify stable selectors.
4. [x] Design SQLite schema and create migrations/schema initialisation.
5. [x] Write parser for part/chapter/rule nodes.
6. [x] Write parser for glossary term nodes.
7. [x] Extract hierarchy and hyperlink edges.
8. [x] Add defined-term usage detection.
9. [x] Generate embeddings and semantic similarity edges.
10. [x] Build graph export and stats report.
11. [x] Build minimal FastAPI API.
12. [x] Build React graph view.
13. [ ] Validate on a manually reviewed sample.

Additional completed expansion work:

- [x] Scrape all current PRA Rulebook Parts.
- [x] Scrape CRR Terms List.
- [x] Scrape Guidance index and guidance document pages.
- [x] Scrape initial Legal Instruments listing page.
- [x] Derive first richer cross-rule edges from shared defined terms.

## 15. File/artifact conventions

Suggested repository layout:

```text
pra-rulebook-explorer/
  rulebookexp.md
  README.md
  backend/
    app/
    tests/
    data/
      raw/
      processed/
  frontend/
  notebooks/
  docs/
  outputs/
```

Keep raw harvested data out of git unless small fixtures are needed for tests. Commit parser fixtures and quality reports where useful.

## 16. Definition of success

The app succeeds if it lets a knowledgeable user move from “I know this part of the Rulebook” to “I can see adjacent, dependent and analogous provisions I would not naturally have checked”, while maintaining enough provenance that every insight can be challenged and verified against the original text.
