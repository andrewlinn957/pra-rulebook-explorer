# Data lifecycle and authority

The explorer has two corpus domains and several deliberately derived projections.
Treating every table as independently authoritative is what previously allowed
search, enrichment, and reporting rows to drift apart.

## Sources of truth

- Files under `backend/data/raw/` are immutable acquisition evidence.
- `document_source`, `node`, and `edge` are the parsed rulebook corpus and graph.
- `source_document`, `source_span`, and the reporting relational tables are the
  parsed reporting facts where that structure exists.
- `graph_node` and `graph_edge` are the reporting serving graph. They also hold
  reviewed semantic nodes that have no relational-table equivalent, so changes
  to them must go through deterministic loaders or recorded review scripts.
- `reporting_template_enrichment` is attached to a `Template` in `graph_node`.
  It is not owned by the smaller relational `template` table.

## Rebuildable projections

- `canonical_guidance_*` and `canonical_node` select the preferred rulebook
  representation.
- `node_fts` is the rulebook full-text search index.
- `embedding` and similarity edges are optional semantic-search products.

Node changes mark the canonical/search projection as dirty. API startup rebuilds
dirty canonical and FTS projections. The explicit maintenance command does the
same and then checks all supported invariants.

## Mutation contract

1. Back up the SQLite database before a destructive or bulk cleaning pass.
2. Open writable connections through `backend.app.db.connect`, or at minimum
   enable `foreign_keys`, a busy timeout, and WAL consistently.
3. Use ordered migrations in `backend.app.migrations`; do not add an unversioned
   `ALTER TABLE` to application startup.
4. Make loaders idempotent and keep provenance/evidence before deleting inputs.
5. After mutation, run:

   ```bash
   .venv/bin/python -m backend.app.cli stabilize
   ```

6. Do not publish or restart the service unless `check-integrity` succeeds.

The integrity gate covers foreign keys, graph endpoints, enrichment ownership,
canonical-node coverage, and FTS coverage. Audit/LLM workflow tables remain
script-owned, but their scripts must not bypass the gate above.

## Reporting graph quality gate

Before claiming the reporting graph is fixed, run the reporting-specific gate:

```bash
.venv/bin/python scripts/validate_reporting_graph_quality.py \
  --db backend/data/rulebook.sqlite3 \
  --raw-root . \
  --report outputs/reporting-graph-quality-report.json \
  --strict
```

Use `--api-base http://127.0.0.1:8100` as well when the backend is running and
API smoke checks are required.

Audit and cleanup facts must stay in internal tables such as
`reporting_node_cleanup`, `source_document_cleanup`, and
`source_document_inspection`. Do not write `audit_cleanup`, model names, prompt
versions, cleanup decisions, or family classifications into `graph_node`
properties or frontend-facing API payloads.

Taxonomy child artefacts must keep distinct source identities. Do not dedupe
XML, XSD, or XBRL children by parent ZIP URL, inherited URL, title, or checksum
alone. Only exact child identity rules may collapse taxonomy children, and the
decision reason must record that exact child-path basis.
