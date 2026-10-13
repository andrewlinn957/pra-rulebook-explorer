**PRA Rulebook Explorer: code, corpus and ontology audit, 13 October 2026**

This report records the initial audit baseline. Follow-up fixes and the current
remaining review items are recorded in “Remediation status” at the end.

The application has useful foundations: preserved source snapshots, canonical provision identities, occurrence records, reporting editions, provenance, and a substantial test suite. It is not yet ready to support the claim that every cross-reference can be expanded inline. The highest-priority problem is actually source-text loss in the reader, followed by broken occurrence associations and incomplete nested traversal.

This audit reviewed the working tree, including the pre-existing uncommitted embedding-retirement changes. Those changes were preserved. New work consists of audit scripts, tests and this report. No production data cleanup, application fixes, service restart, external model calls or source refresh was performed. Corpus experiments used a consistent SQLite backup. Initial test discovery revealed two test files that connect to the default corpus; that run was stopped and pytest was run with their imported default redirected to the backup.

Evidence is saved in [the audit directory](/root/.openclaw/workspace/projects/pra-rulebook-explorer/outputs/codebase-audit-2026-10-13). Counts describe that local snapshot, not the current official Rulebook. This was a software/data audit, not a legal interpretation or certification of source currency.

| Measurement | Result |
|---|---:|
| Rulebook graph | 59,831 nodes; 274,701 edges; 12 edge types |
| Reporting graph | 384,969 nodes; 926,349 edges; 40 edge types |
| Recorded citation occurrences | 45,920, including 45,845 marked materialized |
| Atomic records checked for reader text preservation | 26,485 |
| Records losing non-whitespace content in reader transformation | **542: 24 rules and 518 guidance paragraphs** |
| Materialized occurrences pointing to a deleted edge | **3,875** |
| Occurrences whose source/target differs from the recorded edge | **298** |
| Invalid or out-of-bounds source spans | **946**, all in structural/aggregate records |
| Materialized occurrences targeting no direct text or readable rule version | 7,266; includes structural containers and external documents |
| Recall scanner | 29,737 records; 99,851 candidates; **8,581 require review** |
| Reader placement sample | 36 reference-heavy atomic records; 1,629 groups; 563 unplaced |

The denominators above are different. A graph edge is a relationship, an occurrence is a position in source text, and a reader group may combine several targets. Structural records also repeat child text. None of these ratios is a measured corpus-wide reference recall percentage.

**Findings, in priority order**

1. **[P1] Long passages lose substantive text before citations are placed.** In [sentenceChunks](/root/.openclaw/workspace/projects/pra-rulebook-explorer/frontend/src/readingMode.js:20), the sentence regex excludes every full stop from the text body but only accepts punctuation as an ending when followed by whitespace/end. Decimal references therefore cause preceding text to be skipped. The fallback runs on prose longer than 1,100 characters. Insurance Company – Reporting 14.3, source ID `6e64aadbaedc3415`, contains 2,306 characters; its reader blocks reconstruct only `6`. A synthetic repeated passage containing `rule 2.3` and `Article 4.5` reproduces the loss. The full atomic-corpus comparison found 542 non-whitespace content losses, plus 53 spacing-only differences. Fix splitting so every source character remains represented, preserve tables/list structure where available, and require an automated content-preservation invariant. Evidence: [full results](/root/.openclaw/workspace/projects/pra-rulebook-explorer/outputs/codebase-audit-2026-10-13/reader-text-preservation.json) and [reproducer](/root/.openclaw/workspace/projects/pra-rulebook-explorer/outputs/codebase-audit-2026-10-13/reader-text-loss.json).

2. **[P1] Occurrence records are detached from the surviving graph edges.** There are 3,875 materialized occurrences referencing nonexistent edge IDs: 3,492 on rules and 383 on guidance paragraphs. All have another `references` edge between the same endpoints, so this is not proof that all 3,875 citations are invisible. However, [the reader attaches occurrences by edge ID](/root/.openclaw/workspace/projects/pra-rulebook-explorer/backend/app/graph.py:150); the surviving relationship does not inherit these records automatically. Another 298 records disagree with their edge endpoints. The existing legal-identity audit already fails for the missing edges, while the general integrity gate passes. Repair associations using source/target/evidence identity, preserve multiple occurrences, and make occurrence integrity part of the publication gate. Endpoint-pair equality alone is insufficient where different evidence or relationship types exist. Evidence: [occurrence audit](/root/.openclaw/workspace/projects/pra-rulebook-explorer/outputs/codebase-audit-2026-10-13/cross-reference-integrity.json), [breakdown](/root/.openclaw/workspace/projects/pra-rulebook-explorer/outputs/codebase-audit-2026-10-13/reference-breakdown.json).

3. **[P1] Nested references stop at canonical provisions.** [reader_bundle](/root/.openclaw/workspace/projects/pra-rulebook-explorer/backend/app/graph.py:716) traverses outgoing edges using canonical target IDs, then fills their text from dated rule versions only after traversal. Those versions own their outgoing citations. Reproduction: A references canonical B; B's version says “See rule 3” and references C. At reference depth 2 the bundle displays B's text but returns only A→B. Frontend nested lookup also uses B's canonical ID. Resolve the dated version before each traversal step, and carry the version's outgoing occurrences into the displayed node context. HTML occurrences currently attach only for the initial reading sources and need the same treatment at deeper levels. The corpus contains 5,209 canonical provisions whose versions have outgoing references; this is the potentially affected population, not a count of failed user sessions. Evidence: [minimal reproduction](/root/.openclaw/workspace/projects/pra-rulebook-explorer/outputs/codebase-audit-2026-10-13/reader-regressions.json).

4. **[P1] Rejected reporting assertions are still returned as graph relationships.** Of 486,888 `REPORTS_CONCEPT` edges, 300,360 are `rejected_candidate` and 178,013 are unreviewed `semantic_keyword` candidates. There are 301,284 rejected edges across all reporting types. [reporting_neighbourhood](/root/.openclaw/workspace/projects/pra-rulebook-explorer/backend/app/reporting.py:2424) does not filter review status. Reproduced with `datapoint:template:FINREP:FINREP_1.1:r0010:c0010`: the neighbourhood returns four rejected concept links, including `concept:OwnFunds`. These are available through the serving API even if some UI views hide technical relationships. Introduce a shared publication policy that excludes rejected assertions, separates proposed inferences from accepted evidence, and is applied by every graph/detail/impact reader. Do not globally exclude all `candidate` rows: some deterministic workbook structure currently uses that status, so classify assertion origin and approval separately first. Evidence: [API-function reproduction](/root/.openclaw/workspace/projects/pra-rulebook-explorer/outputs/codebase-audit-2026-10-13/rejected-edge-reproduction.json).

5. **[P1] Coverage labels do not establish inline coverage, and edge fallback loses repetitions.** [classify_candidate](/root/.openclaw/workspace/projects/pra-rulebook-explorer/scripts/reference_recall_audit.py:746) counts any existing occurrence as coverage without validating its status, live edge, span or target readability. An edge or old LLM finding can also count as coverage without placement verification. Separately, [fallbackGroups](/root/.openclaw/workspace/projects/pra-rulebook-explorer/frontend/src/readingMode.js:415) collapses edge-only references into one group, and [assignReferencesToParagraphs](/root/.openclaw/workspace/projects/pra-rulebook-explorer/frontend/src/readingMode.js:463) assigns each group once. “See rule 2. See rule 2.” with one valid edge produces only one inline button. The 36-source sample yielded 563 unplaced groups and 28 empty-target members. This sample deliberately selected the most reference-heavy records of each atomic type; duplicate assertions, rendering loss and alternate citation wording contribute, so **563/1,629 must not be advertised as the corpus miss rate**. Track detection, resolution, readable content and rendering separately, preserving every lexical occurrence. Evidence: [reader placement results](/root/.openclaw/workspace/projects/pra-rulebook-explorer/outputs/codebase-audit-2026-10-13/reader-placement.json).

6. **[P2] Empty targets cannot satisfy inline expansion without a distinct handling path.** The 7,266 materialized occurrences without directly readable target text include 4,806 external-reference targets, 717 legal instruments and 1,743 structural containers. Some containers have useful children, but the inline component reads only `selectedNode.text`. A valid external URL also does not mean its contents have been ingested. Load structural target content deliberately; distinguish “identified, external only”, “unresolved/ambiguous”, and “readable inline” in both the audit and UI. The eight currently unresolved occurrence records are filtered out by the reader's materialized-only query rather than being shown as recognised unresolved citations.

7. **[P2] Refresh behaviour does not yet have a dependable end-to-end publication gate.** The ingestion audit reports 17 missing manifest edges; its manifest covers only one completed Actuaries scope and 202 objects. That is limited use of the reconciliation machinery, not evidence of corpus-wide refresh safety. The 946 invalid spans are in 820 chapters, 107 guidance sections and 19 guidance documents; they do not directly establish losses from atomic rule text, but cannot remain valid evidence positions. In addition, [full glossary and CRR terms URLs](/root/.openclaw/workspace/projects/pra-rulebook-explorer/backend/rulebook_scraper/cli.py:209) hard-code different dates, 1 June 2026 and 1 January 2027. [Version resolution](/root/.openclaw/workspace/projects/pra-rulebook-explorer/backend/app/graph.py:810) falls back to the newest version when the requested date has no exact match, which can mix later text into a historical context. Introduce one explicit corpus/as-of policy and a staged refresh that runs all required projections and gates before publication. The current snapshot has only one rule version per canonical provision, so multi-version date behaviour needs dedicated fixtures.

8. **[P2] The new embedding-removal migration breaks supported partial-schema fixtures.** Pytest reports 21 failures, all reaching [_migrate_v12](/root/.openclaw/workspace/projects/pra-rulebook-explorer/backend/app/migrations.py:756) with `sqlite3.OperationalError: no such table: edge`. The migration unconditionally assumes rulebook tables before inspecting schema availability. This is a regression in the pre-existing working-tree changes, not evidence that the already-migrated live database currently fails to start. Guard optional tables/columns, test reporting-only and combined schemas, and update migration expectations that still stop at version 11.

9. **[P2] The advertised test command leaves important tests unrun.** [scripts/test.sh](/root/.openclaw/workspace/projects/pra-rulebook-explorer/scripts/test.sh:7) invokes unittest discovery, which omits 79 plain function tests in files without a unittest adapter, including reader, recall and reconciliation tests. `requirements-dev.txt` also does not declare pytest. Two test files directly import `DEFAULT_DB` and depend on local workbooks, so the default suite is not isolated or reproducible from a clean checkout. Use pytest for the complete suite; separate fixture-only tests from explicit corpus integration tests and make their database path configurable. Many frontend tests inspect source strings rather than rendered behaviour, explaining why the keyboard and text-loss defects escape the green frontend suite.

10. **[P2] The derived-layer rebuild helper can commit a failed cleanup.** [rebuild_derived_layer.py](/root/.openclaw/workspace/projects/pra-rulebook-explorer/scripts/rebuild_derived_layer.py:85) commits before testing `report['ok']`; the subsequent rollback cannot undo that commit. It also copies only the main database file with `shutil.copy2` despite WAL use, and has no registered generators, so “rebuild” currently just deletes a layer. Occurrence and ingestion ownership are not reconciled with those deletions. Do not use this helper for ontology cleanup until it uses a SQLite backup, explicit retirement versus regeneration modes, transactional validation before commit, and dependent-record reconciliation. This finding is from code inspection; the destructive helper was not executed.

11. **[P2] Keyboard users cannot reach the inactive inspector tab.** [The tab markup](/root/.openclaw/workspace/projects/pra-rulebook-explorer/frontend/src/main.jsx:2000) removes the inactive tab from tab order without implementing arrow-key navigation. A browser check on the locally served app confirmed that ArrowLeft left “Selected node” active and Tab skipped “Connections”. Fix keyboard selection and focus management; add browser tests for tabs, reference expansion/collapse and reader return focus. The existing native citation buttons and connection lists are useful accessibility foundations. This was a focused keyboard smoke check, not a complete accessibility conformance assessment. Evidence: [browser result](/root/.openclaw/workspace/projects/pra-rulebook-explorer/outputs/codebase-audit-2026-10-13/browser-smoke.json).

**Recommended ontology cleanup**

The number of edge types alone is not the problem. Reporting editions, instruction coordinates and evidence need more distinctions than the default user interface. Keep a small public navigation model over explicit, documented evidence relationships. Preserve detailed provenance internally.

| Treatment | Relationships/data | Reason |
|---|---|---|
| Keep as core | `contains`, `references`, `uses_defined_term`, `defines`, `has_version`, `sourced_from`; sourced amendments and permissions | Needed for structure, inline reading, legal identity and evidence |
| Keep as reporting core | Requirement/edition/resource hierarchy; template coordinates; instruction references; `INSTRUCTS`; provenance | Supports the reporting estate and traceable change review |
| Retire or isolate from the serving graph | `shares_defined_term` (83,042), `has_obligation_pattern` (25,865), `has_structured_obligation` (5,447), `shares_obligation_pattern` (2,287) | 116,641 derived analysis edges, 42.5% of the rulebook graph; not required for the reader's three relationship types |
| Quarantine/review | Keyword concept mappings and other inferred scope/calculation assertions | 478,373 keyword concept edges are rejected or still candidates; inference must not appear as accepted evidence |
| Rebuild as a projection | Rolled-up child relationships | `rollup_child_edge` accounts for 94,506 edges; useful for summaries but not independent citations, and overlaps the retirement counts above |
| Retain until consumers migrate | Legacy `DataItem`/`ReportingReturn`/`DisclosureSet` and normalized requirement/edition/resource models | Current cell navigation bridges them; indiscriminate deletion would break functionality |

Retirement must disable generators and UI presets as well as remove their current outputs. `derive_richer_edges` still creates shared-term edges, and the enrichment command still creates obligations/shared-obligation edges. The embedding table and similarity edge types are already absent in this snapshot, but the obsolete BGE monitor script remains. Inventory code consumers before removal, keep a backup and review receipt, and verify the reader/reporting views on a candidate copy.

There are also 110,962 additional `REPORTS_CONCEPT` rows and 87,593 additional `HAS_DATAPOINT` rows beyond unique source/type/target triples. These are duplicate *topologies*, not automatically duplicate evidence. Consolidate the serving relationship while preserving separate evidence assertions and review history.

**A repeatable reference-coverage contract**

For each published source snapshot, record source hash, extraction version and the intended corpus date. Each citation should have a stable occurrence ID, exact source span/group, original wording, target identity/version, evidence method and an explicit status. The reader should account for every reviewed occurrence as inline expandable, recognised but unresolved/ambiguous, external-only, or reviewed non-reference. It should never silently drop one.

Measure each stage separately:

1. Acquisition/parsing: compare cached source rows and anchors with parsed provisions, including tables, list limbs and footnotes. This audit's cached-HTML structural check found no missing rows under its existing rules; it does not verify original PDF layout, omitted source documents or the current website.
2. Detection: use HTML anchors, deterministic grammars and full-text review as complementary sources. The fresh ledger has 3,084 `needs_review`, 5,438 `tail_unreviewed` and 59 unresolved prior LLM candidates. These 8,581 candidates include overlaps and aggregate copies; adjudicate/deduplicate before counting missing references.
3. Resolution: require live consistent targets/edges, valid spans and readable target content or an explicit non-inline status. Grouped lists/ranges explain some of the additional 8,681 citation/span wording differences, so those require review rather than automatic rejection.
4. Rendering: require preservation of non-whitespace source content; verify each expected occurrence reaches an inline segment; exercise repeated citations, nested expansion, tables, definitions, external instruments and historical versions.
5. Independent recall review: review a stratified, blinded sample of full provisions, including records with no detector candidates and excluded candidates. Build an adjudicated benchmark and report precision/recall on that benchmark. Reviewing only the scanner's positive candidates cannot establish completeness. Absolute 100% natural-language recall is not established by a zero-error regex run.
6. Refresh: fail publication on new broken links or text loss, stale source hashes, unexplained coverage regressions, or failed tests. Re-review changed sources and affected targets; preserve decisions only against unchanged source/evidence versions.

The recall ledger's `--resume` now includes a fingerprint of each source's current edge, occurrence, HTML and LLM evidence. Resume only preserves prior decisions when that fingerprint and the source-text hash are unchanged; otherwise the source is rescanned.

New read-only tools can be run from the project directory. `--strict` deliberately exits 1 for broken occurrence links/spans; success does **not** certify recall:

```bash
.venv/bin/python scripts/audit_cross_reference_integrity.py \
  --db backend/data/rulebook.sqlite3 \
  --output-dir outputs/reference-audit-next \
  --reader-sample-per-type 12 --export-reader-text --strict

node scripts/audit_reader_text.mjs \
  outputs/reference-audit-next/reader-text-corpus.jsonl \
  > outputs/reference-audit-next/reader-text-preservation.json

node scripts/audit_reader_coverage.mjs \
  outputs/reference-audit-next/reader-bundles.jsonl \
  > outputs/reference-audit-next/reader-placement.json

.venv/bin/python scripts/reference_recall_audit.py \
  --db backend/data/rulebook.sqlite3 \
  --ledger outputs/reference-audit-next/recall-ledger.sqlite3 \
  --pilot-output outputs/reference-audit-next/review-pilot.jsonl
```

The two Node checks output measurements and do not enforce a pass/fail threshold. The reader bundle sample is configurable with repeated `--reader-root` arguments; the default command does not test all source bundles. Text preservation and occurrence integrity were checked across their complete declared populations. A 200-source candidate review pilot is saved in the audit directory; it has not been human-adjudicated.

**Verification and implementation order**

| Check | Observed result |
|---|---|
| Existing Python tests, pytest on isolated corpus copy | 295 passed; 21 failed; 19 subtests passed |
| New Python audit tests | 4 passed |
| Existing frontend tests | 126 passed |
| New text-audit tests | 2 passed |
| Frontend production build | Passed; warning for a roughly 520 kB minified JS chunk |
| General database integrity | Passed, eight metrics zero |
| Legal-identity audit | Failed: 3,875 broken occurrence edge associations |
| Ingestion reconciliation audit | Failed: 17 missing manifest edges |
| Reporting quality gate | 17 checks, zero failures, two warning checks concerning CLOSELINKSNOTIFICATIONFORM |
| Cached HTML structural completeness | Zero issues under the existing checker |
| Browser smoke | No page errors; keyboard tab defect reproduced |

Implement in this order: (1) preserve reader text and add behaviour tests, (2) repair occurrence associations and nested canonical/version traversal, (3) enforce publication/review status and expose unresolved/external-only states, (4) make all tests and refresh gates reliable, (5) retire optional analysis generators and projections, then (6) adjudicate the recall ledger and add a maintained benchmark.

For maintainability, split the 2,281-line React entry point into reader, reporting, graph and issue components, and separate reporting queries/projections from the 2,706-line reporting module. Centralize relationship/provenance definitions shared between backend and frontend; definitions already diverge between `EXPLICIT_METHODS`, frontend origin filtering and inferred reporting status. Add explicit database connection lifetimes to request handlers. These are follow-on changes; they should not delay the content-loss fix or replace measurable correctness work.

**Remediation status, 13 October 2026**

| Initial finding | Follow-up change and current evidence |
|---|---|
| Reader text loss and repeated references | Sentence chunking now preserves all non-whitespace source content; repeated edge-only citations are matched at every occurrence, and overlapping duplicate occurrence records for the same target are combined. The full 26,485-record comparison has **zero content changes** (53 spacing-only differences). A fresh three-source reader sample has no unplaced Article references; 10 groups in SS3/21 remain in the visible “Other links” list, and one external target is empty. |
| Broken occurrence associations | SQLite-consistent backups were saved before both repair passes. The first pass reattached 3,297 exact edge/evidence matches and created 876 occurrence-specific edges. A stricter relationship-type check then found 237 further mismatches; the second pass reused one exact edge and created 236 occurrence-specific edges. The refreshed 45,920-occurrence audit and repair transaction report **zero** materialized missing-edge, endpoint or relationship-type mismatches. |
| Nested and dated reading | Reader traversal now uses outgoing links from the dated version selected for the requested as-of date. Version selection prefers an exact date and otherwise uses the latest non-future version; it no longer falls forward to future text. |
| Rejected reporting assertions | Rejected candidates are removed from reporting neighbourhood, return/impact, edge-list and direct edge-detail responses. Semantic keyword and reporting LLM links are identified as inferred in the graph origin filter; other candidate links remain reviewable because some are deterministic structure. |
| Empty and unresolved targets | Materialized, unresolved and ambiguous occurrences are represented in reader responses. The 7,266 targets without direct text/readable versions split into **6,527 URL-backed targets** and **739 structural containers with readable descendants**; the reader opens the source URL or loads contained text inline. The refreshed audit finds no target lacking both readable content and a source URL. |
| Runtime reader verification | Restarted `pra-rulebook-api.service`; `/health` reports the repaired corpus DB and schema version 12. The live reader endpoint returns all 9 direct occurrences for a structural source and a 47-node contents spine, including 46 text-bearing nodes, for an expandable target. |
| Refresh and recall auditing | Glossary and CRR export dates are independent CLI options. Recall-ledger resume now includes each source's current edge, occurrence, HTML and LLM evidence fingerprint, and old “covered” labels have been replaced by “recorded” statuses that do not claim reader placement. The ledger still has **8,581** candidate findings requiring review, including overlaps and aggregate copies; this is not a missed-reference count. |
| Migration, derived cleanup and test discovery | Migration 12 now tolerates partial schemas. Derived-layer application refuses to delete output without a registered replacement generator, and validates before commit using a consistent backup. The default test command uses pytest and excludes corpus/workbook integration checks into a separate command. Follow-up verification: **127 frontend tests passed**, **8 focused backend tests passed**, production build passed, Python compilation passed, and `git diff --check` passed. |
| Accessibility and semantic-analysis residue | Inspector tabs support arrow, Home and End navigation. Pairwise shared-term and obligation-pattern generation and serving presets are retired; historical rows remain in the database for provenance and are hidden from user-facing node, edge, neighbourhood, path and analysis routes. The obsolete BGE monitor script and semantic-cluster color path were removed. |

Four backed-up automatic repair passes adjusted **8,773 occurrence spans** (8,602 body spans and 171 title spans). The current audit leaves **775 out-of-bounds spans** on empty structural sources (772 chapters, two guidance sections and one guidance document) and **12 materialized group/range wording differences**. The structural records have no unique anchor in either body or title; the reader now loads their outgoing links and lists unanchored ones under “Other links.” The 12 grouped cases retain verified source slices, which the reader uses as a fallback clickable anchor. A targeted two-provision sample placed 9 of 10 groups; its one unplaced aggregate external link remains available from the “Other links” list. **67 records explicitly marked `not_reference`** are excluded from reader-span findings rather than auto-shifted.

The 7,266 target-content cases now have reader access paths: **6,527** link to a source URL and **739** load readable contained provisions inline. The initial 26,485-record text-preservation check remains at zero content loss (53 whitespace-only differences). Current integrity counts and sample placement evidence are saved in [the final audit directory](/root/.openclaw/workspace/projects/pra-rulebook-explorer/backend/data/audits/occurrence-repair-final-20261013). The first and last span-repair SQLite snapshots are [here](/root/.openclaw/workspace/projects/pra-rulebook-explorer/backend/data/backups/rulebook.pre-span-repair-20261013T183519367818Z.sqlite3) and [here](/root/.openclaw/workspace/projects/pra-rulebook-explorer/backend/data/backups/rulebook.pre-span-repair-20261013T185829966735Z.sqlite3). The independent human-recall benchmark still has not been adjudicated, so complete natural-language reference recall is not claimed.
