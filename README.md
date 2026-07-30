# PRA Rulebook Explorer

See `rulebookexp.md` for the product and MVP spec.

## Scraper MVP

Install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

Run the full corpus scrape:

```bash
./scripts/scrape_rulebook.sh
```

Outputs:

- SQLite DB: `backend/data/rulebook.sqlite3`
- Raw HTML cache: `backend/data/raw/`
- Graph JSON: `backend/data/processed/graph.json`

List available Rulebook parts:

```bash
.venv/bin/python -m backend.rulebook_scraper.cli index
```

Scrape a specific part:

```bash
.venv/bin/python -m backend.rulebook_scraper.cli scrape \
  --include-glossary \
  --full-glossary \
  --part /pra-rules/internal-liquidity-adequacy-assessment/01-06-2026
```

Scrape every Part linked from `/pra-rules` plus CRR Terms List, Guidance, Legal Instruments, and derived edges:

```bash
.venv/bin/python -m backend.rulebook_scraper.cli scrape \
  --all-parts \
  --include-glossary \
  --full-glossary \
  --include-crr-terms \
  --full-crr-terms \
  --all-guidance \
  --include-legal-instruments \
  --derive
```


## Phase 2 Graph Backend

Build search, embedding and similarity indexes:

```bash
.venv/bin/python -m backend.app.cli build-indexes --embeddings --similar --top-k 5 --threshold 0.72
```

For higher-quality semantic maps, rebuild embeddings with a Hugging Face Sentence Transformers model. Recommended first choice: `BAAI/bge-m3`, because it is strong, open, long-context, and still practical on a CPU VPS when node text is truncated. Sentence-transformer embedding runs default to the first 1600 characters per node; override with `--text-chars` if you need a faster run or have more CPU/RAM headroom.

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m backend.app.cli build-indexes \
  --embeddings \
  --model sentence-transformers:BAAI/bge-m3
.venv/bin/python -m backend.app.cli build-indexes \
  --similar --top-k 5 --threshold 0.62
.venv/bin/python -m backend.rulebook_scraper.cli enrich
systemctl restart pra-rulebook-api
```

Run the API locally:

```bash
./scripts/run_api.sh
```

Default API base: `http://127.0.0.1:8100`

Useful endpoints:

- `GET /health`
- `GET /stats`
- `GET /search?q=operational%20resilience&limit=10`
- `GET /node/{id}`
- `GET /node/{id}/neighbourhood?depth=1&limit=100&explicit_only=false`
- `GET /path?from={id}&to={id}` (also accepts `from_id`/`to_id`)
- `GET /interesting?limit=50`
- `GET /centrality?limit=25`
- `GET /analysis/semantic-map?level=part&clusters=12&edge_limit=700`
- `GET /analysis/semantic-map?level=article&clusters=18&edge_limit=1800`

Verify the backend against the scraped corpus:

```bash
./scripts/verify_backend.py
```

Current full-corpus verification after advanced enrichment: 34,572 nodes, 254,390 edges, 85,768 `similar_to` edges, 26,888 `references` edges, 12 keyword topic nodes, 36 embedding-derived topic clusters, 9,474 obligation-pattern nodes, 4,991 structured obligation-statement nodes, and zero missing edge targets.

Generated corpus artefacts are intentionally excluded from Git: `backend/data/raw/`, `backend/data/processed/`, and `backend/data/*.sqlite3*`. Rebuild them locally with the scrape, enrich and index commands above.

Notes:

- CRR Terms List is fetched at `01-01-2027`, because the live page says it is not effective until then.
- Legal Instruments currently parses the visible listing page. The page advertises more historic results via JS pagination, so a later pass should reverse-engineer that endpoint or crawl year/part filters.
- `--derive` adds deterministic discovery edges, currently shared low/medium-degree defined terms and title-based resolution of dated Part references.

## Phase 3 React UI

Install and build the frontend:

```bash
cd frontend
npm install
npm run build
```

## Stabilization and verification

The database authority and projection lifecycle are documented in
[`docs/data-lifecycle.md`](docs/data-lifecycle.md).

After any bulk ingestion or cleaning pass, apply migrations, rebuild canonical
search projections, and fail on integrity drift:

```bash
.venv/bin/python -m backend.app.cli stabilize
```

For a read-only integrity gate after stabilization:

```bash
.venv/bin/python -m backend.app.cli check-integrity
```

Run the complete backend and frontend verification with one command:

```bash
scripts/test.sh
```

## Reporting estate catalogue

The public reporting view is built from the authoritative PRA reporting tables,
not from filename-family guesses. Rebuild it, inspect workbook sheet names,
download any newly published official files, and refresh its graph projection:

```bash
.venv/bin/python scripts/rebuild_reporting_catalog.py --download-missing
.venv/bin/python scripts/project_reporting_ontology.py
.venv/bin/python scripts/project_reporting_instruction_coordinates.py --apply
```

Useful catalogue endpoints are `GET /reporting/catalog`,
`GET /reporting/catalog/{return_id}` and
`GET /reporting/catalog/{return_id}/cells`. The cell endpoint bridges a
normalized requirement edition to the existing parsed template, row, column
and datapoint corpora, including both normalized relational cells and
graph-native workbook cells. It uses the edition's exact official-template URL
first, with the direct data-item identity reserved for catalogue rows that have
no template artifact. Annex editions can therefore reach their existing
COREP/FINREP cells without filename guessing, stale-version fallback or
cross-resource leakage. Duplicate data-item projections and parallel graph
evidence are collapsed to one logical template and cell. Its `coverage` field
distinguishes cell data that is
available from templates that have not yet been parsed and editions that are
not yet mapped to a parsed template. Pillar 3 disclosures are deliberately a
separate estate from regulatory returns.

`GET /reporting/impact/{target_node_id}` provides the evidence-led foundation
for rule-change applications. It finds reporting instruction sources that
directly reference a changed graph node and returns the supporting passages.
Where the same instruction provision names an explicit row, column or cell, it
returns `direct_coordinate_evidence` separately. The remaining associated
templates and cells are `candidate_scope`. Neither tier is automatically a
confirmed edit: a user must review the instruction passage and the legal change.

The instruction projector is deterministic and idempotent. A run without
`--apply` is read-only. It creates `InstructionProvision` nodes, exact
`REFERENCES_RULE` links only where a canonical legal target exists, and
`INSTRUCTS` links only for explicit coordinates. When an instruction names a
valid row/column pair that the workbook parser did not materialize as a
`DataPoint`, the graph retains it as a visibly
`instruction_defined_not_materialized` reporting coordinate.

The ontology and naming conventions are documented in
[`docs/reporting-estate-ontology.md`](docs/reporting-estate-ontology.md). The
projection separates stable requirements from dated editions, resources and
their components. Contextual resource names inherit from their associated
requirement unless an edition, resource or component supplies an explicit
`display_name` override.

Set or clear a durable human-readable name without editing the database
directly:

```bash
.venv/bin/python scripts/set_reporting_display_name.py requirement requirement:pra115 "PRA115 — Step-in risk assessment"
.venv/bin/python scripts/set_reporting_display_name.py requirement requirement:pra115 --clear
```

Optional descriptions can be generated cheaply through OpenAI Batch. Create
the input, submit it, then import the completed result:

```bash
.venv/bin/python scripts/enrich_reporting_catalog.py --create --model gpt-5-nano
.venv/bin/python scripts/enrich_reporting_catalog.py --submit PATH.jsonl --model gpt-5-nano
.venv/bin/python scripts/enrich_reporting_catalog.py --import-batch BATCH_ID --manifest PATH.manifest.json
```

Feedback submitted in the UI is stored in the feedback queue. The public API
does not expose queue execution; processing, if needed, is an explicit local
maintenance operation.

Run the local UI, with the API running separately on port 8100:

```bash
./scripts/run_api.sh
./scripts/run_frontend.sh
```

Frontend URL: `http://127.0.0.1:5173`

Implemented UI features:

- reporting-estate navigation from official collection and dated requirement
  edition through resources and template components;
- edition-aware cell search with explicit template, row and column coordinates,
  datatype, unit and official-source context;
- rule-change impact tracing from a changed provision to direct instruction
  evidence and separately labelled candidate template/cell scope;
- keyword search across the corpus;
- selected-node neighbourhood graph;
- controls for depth, node cap, edge types and explicit-only mode;
- node detail and source link panel;
- visible edge provenance, confidence and evidence snippets;
- interesting-connections panel;
- central-nodes panel;
- inferred semantic edges shown with dashed styling to distinguish them from explicit provenance-backed edges.


## Richer enrichment and analysis

Add regex references, topic nodes and obligation-pattern nodes/edges:

```bash
.venv/bin/python -m backend.rulebook_scraper.cli enrich
```

Rebuild semantic embeddings with BGE-M3 in the project venv:

```bash
PYTHONPATH=$PWD .venv/bin/python \
  -m backend.app.cli build-indexes \
  --embeddings \
  --model sentence-transformers:BAAI/bge-m3
PYTHONPATH=$PWD .venv/bin/python \
  -m backend.app.cli build-indexes \
  --similar --top-k 5 --threshold 0.62
```

Additional analysis endpoints:

- `GET /analysis/betweenness?limit=25&k=750`
- `GET /analysis/components?limit=20`
- `GET /analysis/communities?limit=20`
- `GET /analysis/common-neighbours?from_id={id}&to_id={id}`


## Public VPS route

The polished desktop UI is deployed through the existing VPS hub at:

- `http://vmi3225794.tail5a515c.ts.net/pra-rulebook`

Runtime pieces:

- static frontend build copied to `/root/.openclaw/workspace/qbit-mini-ui/public/pra-rulebook/`;
- same-origin API proxy at `/pra-rulebook-api/*`;
- persistent backend service: `pra-rulebook-api.service`;
- existing public host service: `qbit-mini-ui.service`.


## License

This software is licensed under the PolyForm Noncommercial License 1.0.0.
It is free to use for non-commercial purposes. Commercial use requires a separate commercial licence from the copyright holder.

See [LICENSE](LICENSE) for the full terms.
