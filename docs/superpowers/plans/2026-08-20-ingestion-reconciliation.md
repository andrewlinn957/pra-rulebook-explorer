# Ingestion output reconciliation implementation plan

## Goal

Ensure that a successful Rulebook ingestion scope represents the complete
current output for that scope. Remove stale page/version nodes and
source-owned edges when they disappear, retain immutable snapshots, and leave
previous live output unchanged when a target fails.

## Architecture

Add a run ledger and latest-output manifest to the scraper SQLite schema and
the application migration. Reconcile one source scope in one transaction after
fetch and parse have succeeded. Track ownership by scope so shared canonical
nodes survive until their last owner disappears. Use a separate deterministic
scope for richer derived edges. Bootstrap legacy ownership from source URLs and
metadata on the first refresh of an existing database.

## Tech Stack

Python 3.12, SQLite, BeautifulSoup, pytest, FastAPI startup migrations,
existing scraper `Node`/`Edge` dataclasses and the current JSON/FTS/canonical
projections.

## User decisions (already made)

Andrew approved removal of a page/version node and its source-owned edges when
a successful refresh no longer emits them. Immutable `document_snapshot`
history must be retained. Failed or partial targets must not trigger deletion.

## Task 1: Add failing store-level reconciliation tests

**Goal:** Define the observable contract before implementing persistence logic.

**Files:**

- Create: `tests/test_ingestion_reconciliation.py`

**Acceptance criteria:**

- A second successful refresh that omits a rule removes the old version node,
  its source-owned edge and attached occurrence, while retaining the snapshot.
- Repeating the same output is idempotent.
- A node shared by two source scopes survives removal from one scope and is
  deleted after removal from the final scope.
- A changed node replaces source-managed metadata and invalidates old source
  occurrences.
- A failed scope leaves its previous manifest and live objects unchanged.
- Legacy data without a manifest can be reconciled on its first refresh.

**Verify:** `pytest -q tests/test_ingestion_reconciliation.py` initially fails
because the reconciliation API and tables are absent.

**Steps:**

1. Build an in-memory scraper schema with a source page, version nodes,
   canonical node, edges, occurrence and snapshot.
2. Write tests against the intended `start_ingestion_run`,
   `reconcile_source_output`, `record_ingestion_scope_failure` and
   `finish_ingestion_run` APIs.
3. Run the focused tests and capture the expected missing-function/schema
   failures before production code is changed.

## Task 2: Implement run ledger, manifests and safe cleanup

**Goal:** Reconcile source output atomically with explicit ownership and
   dependency cleanup.

**Files:**

- Modify: `backend/rulebook_scraper/store.py`
- Modify: `backend/app/migrations.py`
- Modify: `backend/app/db.py`
- Test: `tests/test_ingestion_reconciliation.py`

**Acceptance criteria:**

- Scraper and migrated application databases create the same v9 ingestion
  tables and indexes.
- Reconciliation stores immutable snapshots, replaces current source output,
  and deletes stale edges before stale nodes.
- Shared ownership is respected across source scopes.
- Occurrences, embeddings, FTS rows, canonical rows and aliases are removed or
  invalidated when their live node is removed, with table-existence guards for
  lightweight test schemas.
- A legacy source with no manifest receives a deterministic bootstrap before
  stale comparison.

**Verify:** Focused reconciliation tests pass; migration tests assert schema
  version 9 and idempotence.

**Steps:**

1. Add the ledger/output tables to scraper `SCHEMA` and application migration
   v9.
2. Add deterministic scope keys, payload hashes and run lifecycle helpers.
3. Implement legacy membership discovery from URL, source-page metadata and
   source-owned edges.
4. Implement source reconciliation with live-ID resolution, ownership checks,
   stale-edge deletion, node dependency cleanup and manifest replacement.
5. Add metadata replacement and source-node occurrence invalidation without
   changing legacy callers' default upsert behaviour.
6. Extend index/projection setup so newly migrated databases have the control
   tables and stale cleanup can mark projections dirty.

## Task 3: Wire scraper source scopes and failure isolation

**Goal:** Make the scrape command reconcile only completed targets and report
   partial runs accurately.

**Files:**

- Modify: `backend/rulebook_scraper/cli.py`
- Modify: `tests/test_ingestion_reconciliation.py`
- Test: `tests/test_rulebook_identity_parser.py`

**Acceptance criteria:**

- Each target is fetched and parsed before its source transaction starts.
- Successful targets call source reconciliation with the parsed output and
  snapshot ID; fetch or parse errors record a failed scope and preserve old
  output.
- A run ends `completed`, `partial` or `failed` with counts and errors.
- Targeted invocations cannot prune scopes outside their target list.
- Existing scrape totals, exports and stats continue to work.

**Verify:** Add a CLI-level test using a stub fetcher/parser, then run the
focused scraper tests and an offline dry run against a copied database.

**Steps:**

1. Start a run with the resolved target list and record each scope as running.
2. Move fetch/parse into a guarded per-target block and reconcile only after
   successful parsing and basic output completeness checks.
3. Record failures without committing source or manifest changes for that
   scope, then finish the run with an accurate status.
4. Keep sleep, export and stats behaviour unchanged.

## Task 4: Reconcile richer derived edges

**Goal:** Replace stale deterministic derived edges without deleting parser or
   semantic-reference layers.

**Files:**

- Modify: `backend/rulebook_scraper/enrich.py`
- Modify: `backend/rulebook_scraper/cli.py`
- Test: `tests/test_enrich_source_href_resolution.py`
- Test: `tests/test_ingestion_reconciliation.py`

**Acceptance criteria:**

- `derive_richer_edges` writes one current output manifest under a deterministic
  derived scope.
- Edges removed from a subsequent derivation disappear from the live graph if
  no other scope owns them.
- Only the known richer-edge methods are reconciled; explicit HTML parser
  edges, legal-reference occurrence edges and embedding edges are untouched.
- Existing resolver replacement behaviour remains valid.

**Verify:** Focused enrichment/reconciliation tests pass and a repeated
  derive run produces stable counts and no stale derived edges.

**Steps:**

1. Collect the richer-edge output without committing it first.
2. Reconcile the output under the derived scope after all derivation steps
   complete successfully.
3. Preserve the existing placeholder and resolver handling, then run focused
   tests for replaced and removed anchor edges.

## Task 5: Audit, deploy and verify

**Goal:** Apply the tested change to the serving corpus without losing the
   existing untracked worktree artefacts.

**Files:**

- Create: `scripts/audit_ingestion_reconciliation.py`
- Create: `tests/test_audit_ingestion_reconciliation.py`
- Modify: `docs/data-lifecycle.md`

**Acceptance criteria:**

- The audit reports run/scope status, manifest counts, stale endpoint count,
   orphan manifest membership and snapshot retention.
- A copied database survives a controlled remove-and-refresh scenario with
   zero stale live endpoints and retained old snapshot HTML.
- Full backend tests, frontend tests, production build and integrity checks
   pass.
- The reconciliation implementation is committed and pushed without adding
   local backups or generated output directories.

**Verify:** Run focused tests, then the full suites and live/copy audits before
claiming completion.

**Steps:**

1. Add the read-only audit and document the operational refresh/rollback
   behaviour.
2. Back up the serving SQLite database to an explicit path and run the
   controlled reconciliation test against the copy.
3. Apply migration v9 and run integrity/projection checks on the live database.
4. Run the full verification gates, inspect `git diff --check`, commit only
   tracked source/docs/tests and push `main`.
