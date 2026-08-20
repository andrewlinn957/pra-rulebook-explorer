# Legal identity and provision versioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PRA Rulebook provision identity date-independent while retaining dated text versions and immutable source snapshots.

**Architecture:** Add shared identity-key helpers, emit canonical `provision` nodes alongside dated `rule` version nodes, preserve dated Parts as source pages, and store immutable page snapshots separately. Migrate the current SQLite corpus transactionally, normalise semantic targets to canonical provision nodes, and collapse version IDs in graph-analysis projections while leaving the reader version-oriented.

**Tech Stack:** Python 3.12, SQLite, FastAPI, BeautifulSoup, NetworkX, pytest/unittest, React/Vite and Node’s built-in test runner.

**User decisions (already made):** Andrew approved the full graph hierarchy: canonical provision, provision version and source page/snapshot, with canonical/version layers available to the API but the normal reader remaining version-oriented.

---

### Task 1: Add deterministic legal identity keys

**Goal:** Provide one shared implementation for date-free canonical keys, dated version keys, source-page keys and immutable snapshot IDs.

**Files:**
- Create: `backend/rulebook_scraper/legal_identity.py`
- Test: `tests/test_legal_identity.py`

**Acceptance Criteria:**
- [ ] `https://www.prarulebook.co.uk/pra-rules/liquidity-coverage-ratio-crr/01-06-2026` and the same path dated `01-07-2026` produce the same date-free Part/canonical locator.
- [ ] The two dates produce distinct version keys, and an undated URL produces a deterministic `undated:<snapshot-id>` version suffix.
- [ ] Structural locators retain Chapter/HTML disambiguators and do not treat a bare rule number as globally unique.
- [ ] Snapshot IDs are stable for identical URL/content pairs and change when content changes.

**Verify:** `pytest -q tests/test_legal_identity.py` → all tests pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** for `canonical_part_key`, `canonical_provision_key`, `provision_version_key`, `source_page_key`, `normalise_rulebook_date` and `snapshot_id`, using both dated URLs and an undated URL.
- [ ] **Step 2: Run the focused test** and confirm it fails because the helpers do not exist.
- [ ] **Step 3: Implement the helpers** using `urllib.parse`, a strict trailing `DD-MM-YYYY` matcher and the existing SHA-1 convention.
- [ ] **Step 4: Run the focused test** again and confirm all identity cases pass.

### Task 2: Emit canonical/version nodes and immutable source snapshots

**Goal:** Make every new PRA Part scrape produce one canonical provision, one dated version and explicit source provenance without changing the existing reader’s structural spine.

**Files:**
- Modify: `backend/rulebook_scraper/parse.py`
- Modify: `backend/rulebook_scraper/store.py`
- Modify: `backend/rulebook_scraper/cli.py`
- Test: `tests/test_rulebook_identity_parser.py`

**Acceptance Criteria:**
- [ ] Parsing two dated versions of the same synthetic Part yields one `provision` node, two `rule` nodes with `identity_type='provision_version'`, two `has_version` edges and version metadata pointing to the dated source page and snapshot.
- [ ] Existing `contains` edges still place the version Rule below the Part/Chapter, so `reader_bundle` can use the version text.
- [ ] `document_snapshot` stores one immutable row per URL/content hash and does not duplicate unchanged content.
- [ ] A changed fetch at the same URL creates a second snapshot while the logical `document_source` row remains current.
- [ ] Existing non-Rule parser outputs remain unchanged.

**Verify:** `pytest -q tests/test_rulebook_identity_parser.py tests/test_reader_bundle.py` → all tests pass.

**Steps:**

- [ ] **Step 1: Write failing parser and snapshot tests** with minimal HTML containing one Chapter and one numbered row, parsed once for `01-06-2026` and once for `01-07-2026`.
- [ ] **Step 2: Run the focused tests** and confirm the parser currently emits two dated Rule identities and the store has no snapshot table.
- [ ] **Step 3: Add the identity metadata and nodes** in `extract_part`, using the shared helpers. Keep `node_type='rule'` for versions and add `provision`, `has_version` and `sourced_from` only for the new identity layer.
- [ ] **Step 4: Add `document_snapshot` to the scraper schema** and insert immutable snapshots from `upsert_source` before updating the current `document_source` row.
- [ ] **Step 5: Run the focused tests** and confirm parser, idempotency and reader-spine assertions pass.

### Task 3: Migrate the existing corpus transactionally

**Goal:** Convert the current dated Rule nodes into version nodes, create canonical provisions, retain aliases and remap every dependent endpoint without losing evidence.

**Files:**
- Create: `backend/app/legal_identity_migration.py`
- Modify: `backend/app/migrations.py`
- Modify: `backend/app/db.py`
- Test: `tests/test_legal_identity_migration.py`

**Acceptance Criteria:**
- [ ] Migration 8 creates `document_snapshot` and `node_alias` and is idempotent on a second run.
- [ ] Every migrated Rule has a date-free canonical provision, a version key, `identity_type='provision_version'`, source-page metadata and a `has_version`/`sourced_from` relationship.
- [ ] Old Rule IDs and stable keys are present in `node_alias`; edges, reference occurrences, embeddings and JSON metadata point to live IDs.
- [ ] Rule-targeting semantic edges point to canonical provision IDs while structural `contains` edges continue to point to version IDs.
- [ ] A conflicting key aborts the transaction with no partial migration.

**Verify:** `pytest -q tests/test_legal_identity_migration.py tests/test_migrations.py` → all tests pass, including the second-run idempotency assertion.

**Steps:**

- [ ] **Step 1: Write failing migration tests** using a temporary SQLite schema with two dated Rule nodes, one reference edge, one occurrence, one embedding and nested metadata containing the old IDs.
- [ ] **Step 2: Run the tests** and confirm schema version 8 and the migration function are absent.
- [ ] **Step 3: Implement the migration in one transaction**: create the two support tables, calculate the old-to-new ID map, insert aliases/canonical nodes, remap node IDs and dependent tables, merge canonicalised semantic edges, and update occurrence edge IDs.
- [ ] **Step 4: Add migration 8 to `apply_migrations`** and make `ensure_indexes` rebuild canonical/FTS projections after node changes.
- [ ] **Step 5: Run the focused tests** twice against the same temporary database and confirm no duplicate nodes, edges or snapshots are produced.

### Task 4: Expose canonical identity and preserve reader behaviour

**Goal:** Make API graph analysis operate on canonical legal entities while the normal Part reader continues to render dated version text.

**Files:**
- Modify: `backend/app/graph.py`
- Modify: `backend/app/unified.py`
- Modify: `backend/app/validation.py`
- Modify: `frontend/src/main.jsx`
- Modify: `frontend/src/graphPresentation.js`
- Test: `tests/test_legal_identity_graph.py`
- Test: `frontend/src/legalIdentity.test.mjs`

**Acceptance Criteria:**
- [ ] Node payloads expose canonical provision ID, version date, source page ID and snapshot ID when present.
- [ ] The default reader contents contain only dated source-page/version nodes and retain the selected version text.
- [ ] Graph-analysis projections map version IDs to one canonical provision ID before calculating connected components, centrality, communities and semantic aggregates.
- [ ] `provision`, `has_version` and `sourced_from` are labelled and selectable without being added to the default reading representation.
- [ ] Existing guidance, external-reference and reporting API behaviour remains unchanged.

**Verify:** `pytest -q tests/test_legal_identity_graph.py` and `npm test` from `frontend` → all tests pass.

**Steps:**

- [ ] **Step 1: Write failing API/analysis tests** for canonicalised NetworkX node IDs, reader version content, metadata decoration and frontend labels/source conventions.
- [ ] **Step 2: Run the focused tests** and capture the current duplicate version IDs and missing identity fields.
- [ ] **Step 3: Add a graph identity projection** that replaces version IDs with canonical IDs only for analysis, retaining source/version IDs in evidence metadata.
- [ ] **Step 4: Decorate rulebook and unified node payloads** with identity metadata and add the three new relationship labels/colours to the frontend without changing default edge selections.
- [ ] **Step 5: Run focused tests, then the complete Python and frontend suites.**

### Task 5: Migrate and verify the live local corpus

**Goal:** Apply the tested model to a backup copy first, then the local serving database, and leave a reproducible audit of identity and snapshot invariants.

**Files:**
- Create: `scripts/audit_legal_identity.py`
- Modify: `docs/data-lifecycle.md`
- Test: `tests/test_audit_legal_identity.py`

**Acceptance Criteria:**
- [ ] The audit reports zero Rule nodes whose stable key is a dated legacy `rule:part:<dated-path>` key.
- [ ] Each canonical provision has one or more version nodes, and each version has a source page and snapshot.
- [ ] No live edge or materialised occurrence points to a missing node or stale pre-migration ID.
- [ ] The audit reports the count of canonical provisions, versions, source pages and snapshots, plus any unresolved identity conflicts.
- [ ] `python -m backend.app.cli stabilize --db <copy>` and `python -m backend.app.cli check-integrity --db <live-db>` complete successfully.

**Verify:** `pytest -q tests/test_audit_legal_identity.py`, then run the copy/live commands in the task handoff and inspect the JSON audit for zero invariant failures.

**Steps:**

- [ ] **Step 1: Write failing audit tests** for the zero-legacy-key, complete-version-provenance and no-stale-endpoint checks.
- [ ] **Step 2: Implement the read-only audit** and add the data-lifecycle runbook commands.
- [ ] **Step 3: Back up the current SQLite database** to an explicit timestamped path and run migration/stabilisation against the copy.
- [ ] **Step 4: Run the audit against the copy** and compare node/edge/occurrence counts with the pre-migration baseline.
- [ ] **Step 5: Apply the same migration to the live local database**, rebuild indexes, run integrity checks and restart the serving backend only if required.
- [ ] **Step 6: Run the API smoke checks** for a canonical provision, a dated version, the reader bundle and the graph-analysis projection.
