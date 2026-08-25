# Reference-recall SQLite connection contract implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the reference-recall audit, review and staging tools by making every standalone SQLite caller use the shared connection helper contract while preserving read-only source databases.

**Architecture:** `scripts/safe_connect.py` remains the single boundary for standalone script connections. Callers pass filesystem paths and semantic options such as `readonly=True`; only the helper constructs SQLite URI strings. The audit source opener continues to enforce `PRAGMA query_only=ON`.

**Tech Stack:** Python 3.12, `sqlite3`, pytest.

**User decisions (already made):** none.

---

## Root cause

`scripts/safe_connect.py::connect` accepts `path`, `readonly` and `timeout`; it does not accept the native `sqlite3.connect(..., uri=True)` keyword. `scripts/reference_recall_audit.py::connect_source` currently passes both a manually constructed `file:...` URI and `uri=True`, so all eight reported failures stop at the shared source opener before exercising their actual behaviour. A repository search also found other standalone scripts using the same stale calling convention.

### Task 1: Add connection-boundary regression tests

**Goal:** Make the intended helper contract and read-only behaviour executable before changing callers.

**Files:**
- Create: `tests/test_safe_connect.py`
- Modify: `tests/test_reference_recall_audit.py`

**Acceptance Criteria:**
- [ ] The shared helper accepts a filesystem path with `readonly=True` and returns rows through `sqlite3.Row`.
- [ ] A read-only helper connection can read but cannot write.
- [ ] `reference_recall_audit.connect_source` opens a temporary source database and leaves it query-only.

**Verify:**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_safe_connect.py \
  tests/test_reference_recall_audit.py
```

**Steps:**

- [ ] **Step 1: Write the failing helper-contract test** in `tests/test_safe_connect.py`:

```python
import sqlite3

import pytest

from scripts.safe_connect import connect


def test_readonly_connection_accepts_a_path_and_rejects_writes(tmp_path):
    db = tmp_path / "source.sqlite3"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE sample(value TEXT NOT NULL)")
    raw.execute("INSERT INTO sample VALUES ('ok')")
    raw.commit()
    raw.close()

    conn = connect(db, readonly=True, timeout=60)
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("SELECT value FROM sample").fetchone()["value"] == "ok"
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO sample VALUES ('blocked')")
    conn.close()
```

- [ ] **Step 2: Add an explicit `connect_source` contract test** beside the existing temporary source database tests in `tests/test_reference_recall_audit.py`:

```python
def test_connect_source_uses_the_shared_readonly_contract(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    make_source_db(source_path, text="See Article 435 of the UK CRR.")

    conn = reference_recall_audit.connect_source(source_path)
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    assert conn.execute("SELECT title FROM node").fetchone()["title"] == "2.4"
    conn.close()
```

- [ ] **Step 3: Run the two test files and record the expected red result.** The `connect_source` test and existing source-opening tests should fail at the current `uri=True` call before the implementation change.

### Task 2: Repair the shared source opener

**Goal:** Fix the root call-site contract without weakening source-database immutability.

**Files:**
- Modify: `scripts/reference_recall_audit.py:255-263`

**Acceptance Criteria:**
- [ ] `connect_source(path)` passes a filesystem path to `safe_connect.connect`.
- [ ] Non-memory source databases use `readonly=True` and `timeout=60`.
- [ ] `:memory:` continues to work without read-only URI mode.
- [ ] `PRAGMA query_only=ON` and the existing busy timeout remain unchanged.

**Steps:**

- [ ] **Step 1: Replace the stale URI call** with the helper's semantic API:

```python
def connect_source(path: Path | str) -> sqlite3.Connection:
    if str(path) == ":memory:":
        conn = connect(":memory:")
    else:
        conn = connect(Path(path).resolve(), readonly=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn
```

- [ ] **Step 2: Do not change the helper signature or add a second URI-handling API.** The existing helper already owns URI construction; its callers should use its path-plus-options contract.

- [ ] **Step 3: Run the boundary tests** and confirm the original eight call paths can now open their source databases.

### Task 3: Remove the stale calling convention from all standalone scripts

**Goal:** Prevent the same contract drift from reappearing in adjacent reference-recall workflows.

**Files:**
- Modify: `scripts/review_reference_recall_corpus.py`
- Modify: `scripts/stage_structural_context_references.py`
- Modify: `scripts/stage_legal_context_references.py`
- Modify: `scripts/build_reference_adjudication_queue.py`
- Modify: `scripts/materialize_legal_context_targets.py`
- Modify: `scripts/stage_recommended_reference_order.py`
- Modify: `scripts/llm_structural_context_pass.py`
- Modify: `scripts/audit_node_quality.py`

**Acceptance Criteria:**
- [ ] Every call importing `safe_connect.connect` passes a filesystem path, not a manually constructed `file:` URI.
- [ ] Read-only calls use `connect(path, readonly=True, timeout=...)`.
- [ ] Writable calls use `connect(path, timeout=...)` without `uri=True`.
- [ ] `scripts/safe_connect.py` is the only standalone helper layer that calls `sqlite3.connect(..., uri=True)`.

**Steps:**

- [ ] **Step 1: Normalize read-only calls**. For example, change:

```python
connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=60)
```

to:

```python
connect(path, readonly=True, timeout=60)
```

- [ ] **Step 2: Apply the same conversion to each listed review, queue, staging, materialisation, LLM-context and quality-audit connection.** Preserve each existing timeout and preserve which databases are read-only versus writable.

- [ ] **Step 3: Confirm no stale helper call remains:**

```bash
rg -n 'connect\\([^\\n]*uri=True' scripts --glob '*.py' --glob '!safe_connect.py'
```

Expected output: no matches.

### Task 4: Verify the complete repair and isolate the commit

**Goal:** Demonstrate that the eight failures are fixed and no unrelated reader work is accidentally included.

**Files:**
- Test: `tests/test_safe_connect.py`
- Test: `tests/test_reference_recall_audit.py`
- Test: `tests/test_import_reference_recall_reviews.py`
- Test: `tests/test_reference_recall_stage.py`
- Test: `tests/test_review_reference_recall_corpus.py`
- Test: full backend suite

**Acceptance Criteria:**
- [ ] The eight previously failing tests pass.
- [ ] The full backend suite has zero failures; the two deprecation warnings may remain.
- [ ] Python compilation and whitespace checks pass.
- [ ] Existing uncommitted reader/parser changes remain untouched and are not included in the connection-contract commit.

**Verify:**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_safe_connect.py \
  tests/test_reference_recall_audit.py \
  tests/test_import_reference_recall_reviews.py \
  tests/test_reference_recall_stage.py \
  tests/test_review_reference_recall_corpus.py

PYTHONPATH=. .venv/bin/python -m pytest -q

PYTHONPATH=. .venv/bin/python -m py_compile \
  scripts/safe_connect.py \
  scripts/reference_recall_audit.py \
  scripts/review_reference_recall_corpus.py \
  scripts/stage_structural_context_references.py \
  scripts/stage_legal_context_references.py \
  scripts/build_reference_adjudication_queue.py \
  scripts/materialize_legal_context_targets.py \
  scripts/stage_recommended_reference_order.py \
  scripts/llm_structural_context_pass.py \
  scripts/audit_node_quality.py

git diff --check
```

- [ ] **Step 1: Run the focused suite.** Expected: all tests pass, including the eight formerly failing tests.
- [ ] **Step 2: Run the full backend suite.** Expected: zero failures and only the existing FastAPI deprecation warnings.
- [ ] **Step 3: Review the diff and status.** Stage only the connection helper, the normalized standalone callers and their tests; do not use `git add .` because the reader/parser changes are already uncommitted.
- [ ] **Step 4: Commit the isolated repair** with:

```bash
git add scripts/reference_recall_audit.py \
  scripts/review_reference_recall_corpus.py \
  scripts/stage_structural_context_references.py \
  scripts/stage_legal_context_references.py \
  scripts/build_reference_adjudication_queue.py \
  scripts/materialize_legal_context_targets.py \
  scripts/stage_recommended_reference_order.py \
  scripts/llm_structural_context_pass.py \
  scripts/audit_node_quality.py \
  tests/test_safe_connect.py tests/test_reference_recall_audit.py
git commit -m "fix: align reference recall SQLite connections"
```

Do not push until separately authorised.
