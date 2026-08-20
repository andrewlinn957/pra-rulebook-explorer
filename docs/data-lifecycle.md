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
- `provision` nodes are date-free canonical legal identities. Text-bearing
  `rule` nodes are dated provision versions and retain the source `part` page
  through `has_version` and `sourced_from` edges. `document_snapshot` is the
  immutable URL/content record used by those versions; `document_source`
  remains the current logical-page cache.
- `node_alias` preserves pre-versioning Rule IDs and stable keys so old links
  can be resolved after migration.
- `ingestion_run`, `ingestion_run_scope`, and `ingestion_output` record the
  latest successful output of each scraper scope. A successful refresh
  reconciles that output and removes stale page/version nodes and
  source-owned edges; failed scopes retain their previous live output.
- `InstructionProvision`, `ReportingCoordinate`, and their projector-owned
  edges are rebuilt from `instruction`, `source_span`, template dimensions and
  exact materialized legal keys by
  `scripts/project_reporting_instruction_coordinates.py`.

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

### Rulebook refresh reconciliation

The scraper treats each fetched URL and source type as an independent scope.
It parses the complete response before replacing that scope's manifest. Fetch
or parse failures are recorded in the run ledger and do not prune the previous
scope output. A successful refresh removes omitted page/version nodes and
source-owned edges when no other scope still owns them. The corresponding
HTML remains in `document_snapshot` and can be replayed for recovery.

After a refresh, inspect the reconciliation audit before publishing the graph:

```bash
PYTHONPATH=. .venv/bin/python scripts/audit_ingestion_reconciliation.py \
  --db backend/data/rulebook.sqlite3 \
  --output outputs/ingestion-reconciliation-audit.json
```

Targeted `--part` or `--guidance` runs reconcile only the requested scopes.
Use `--all-parts` when the successful Rulebook index should be authoritative
for the complete Part catalogue. The immutable snapshot history is retained
even when the live graph removes a stale version.

The integrity gate covers foreign keys, graph endpoints, enrichment ownership,
canonical-node coverage, and FTS coverage. Audit/LLM workflow tables remain
script-owned, but their scripts must not bypass the gate above.

## Numbered Article references

Run the deterministic Article-reference materialiser after a Rulebook or
guidance refresh. It scans atomic provisions, expands joined lists and ranges,
resolves named/same-Part references, and uses the latest revised UK CRR XML for
external Article text:

```bash
curl --fail --location \
  https://www.legislation.gov.uk/eur/2013/575/data.xml \
  --output backend/data/raw/uk-crr/regulation.xml
.venv/bin/python scripts/backfill_uk_crr_article_references.py
.venv/bin/python scripts/backfill_uk_crr_article_references.py --apply
.venv/bin/python scripts/backfill_uk_crr_article_references.py \
  --audit-output outputs/uk-crr-article-reference-zero-gap-audit.json
```

Dry-run is the default. Do not apply when `review_required` or
`range_expansion_errors` is non-zero. After apply, `missing_before_apply` must
be zero on the final dry run. Reviewed anaphoric or truncated-source exceptions
belong in `config/article_reference_classification_overrides.json`, with the
instrument and rationale recorded. The materialiser owns only edges whose
source method ends in `article_reference_v2`; it prunes stale edges in that
owned set and preserves independently sourced links.

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

After instruction or template ingestion, preview and then replace the
instruction-coordinate projection:

```bash
.venv/bin/python scripts/project_reporting_instruction_coordinates.py
.venv/bin/python scripts/project_reporting_instruction_coordinates.py --apply
```

The preview is read-only. Apply deletes and recreates only graph records owned
by this projector in one transaction.

Taxonomy child artefacts must keep distinct source identities. Do not dedupe
XML, XSD, or XBRL children by parent ZIP URL, inherited URL, title, or checksum
alone. Only exact child identity rules may collapse taxonomy children, and the
decision reason must record that exact child-path basis.

## Legal identity migration

Run the migration on a copied database first. The command rebuilds canonical
and FTS projections and fails if any endpoint or identity invariant is broken:

```bash
cp backend/data/rulebook.sqlite3 backups/rulebook-pre-identity.sqlite3
PYTHONPATH=. .venv/bin/python -m backend.app.cli \
  --db backups/rulebook-pre-identity.sqlite3 stabilize
PYTHONPATH=. .venv/bin/python scripts/audit_legal_identity.py \
  --db backups/rulebook-pre-identity.sqlite3
```

The audit must report zero dated legacy Rule keys, missing canonical/version
links, missing snapshots, stale endpoints and orphan aliases. After the copy
passes, repeat the same `stabilize` and audit commands for
`backend/data/rulebook.sqlite3`, then run the API smoke checks for a Part, a
dated Rule version and the graph-analysis endpoints. The normal reader uses
the dated version text; canonical provisions are used for identity and graph
analysis.
