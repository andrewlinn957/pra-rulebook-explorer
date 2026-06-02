# PRA Rulebook Explorer

See `rulebookexp.md` for the product and MVP spec.

## Scraper MVP

Install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
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

For higher-quality semantic maps, rebuild embeddings with a Hugging Face Sentence Transformers model. Recommended first choice: `BAAI/bge-m3`, because it is strong, open, long-context, and still more practical than 1.5B/7B embedding models on a CPU VPS.

```bash
.venv/bin/pip install sentence-transformers
.venv/bin/python -m backend.app.cli build-indexes \
  --embeddings \
  --model sentence-transformers:BAAI/bge-m3 \
  --similar --top-k 8 --threshold 0.66
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

Run the local UI, with the API running separately on port 8100:

```bash
./scripts/run_api.sh
./scripts/run_frontend.sh
```

Frontend URL: `http://127.0.0.1:5173`

Implemented UI features:

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

Rebuild semantic embeddings with local MiniLM, using the shared sentence-transformers venv on this VPS:

```bash
PYTHONPATH=$PWD /root/.openclaw/workspace/.venv-sentence-transformers/bin/python \
  -m backend.app.cli build-indexes \
  --embeddings \
  --model sentence-transformers:sentence-transformers/all-MiniLM-L6-v2 \
  --similar --top-k 5 --threshold 0.72
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

