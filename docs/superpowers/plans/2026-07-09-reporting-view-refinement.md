# Reporting View Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the reporting view easier to scan in overview and more useful in return drilldown/inspector.

**Architecture:** Keep the current React reporting view, backend endpoint and graph layout. Add focused client-side helpers for reporting overview summaries and drilldown grouping, then render those helpers in the existing rail and inspector components.

**Tech Stack:** React, Vite, Node test runner, source-inspection frontend tests.

**User decisions (already made):** Andrew said no mockups. Andrew approved doing improvements 1 and 2: overview scanability and selected return drilldown/inspector usefulness.

---

### Task 1: Improve overview return rows

**Goal:** Return rows expose useful metadata so the overview is more informative before drilling in.

**Files:**
- Modify: `frontend/src/main.jsx`
- Test: `frontend/src/quality-dashboard.test.mjs`

**Acceptance Criteria:**
- [ ] Overview rows use a reporting-specific summary helper.
- [ ] Summary can include template count, source count, submission system and reporting domain where present.
- [ ] Empty metadata degrades to “Open return drilldown”.

**Verify:** `npm test` from `frontend` passes.

**Steps:**
- [ ] Add failing source-inspection test for `reportingReturnSummary`.
- [ ] Implement `reportingReturnSummary(node)` in `main.jsx`.
- [ ] Use it in the overview return list.
- [ ] Run frontend tests.

### Task 2: Group drilldown rail content

**Goal:** Selected return drilldown groups templates, instructions, sources, rules/legal basis, concepts/scope and datapoints instead of showing a flat related-node list.

**Files:**
- Modify: `frontend/src/main.jsx`
- Test: `frontend/src/quality-dashboard.test.mjs`

**Acceptance Criteria:**
- [ ] Drilldown rail derives grouped related artefacts client-side.
- [ ] Empty groups are hidden.
- [ ] Return root and back navigation remain unchanged.

**Verify:** `npm test` from `frontend` passes.

**Steps:**
- [ ] Add failing source-inspection test for `reportingRailGroups` and grouped rendering.
- [ ] Implement `reportingRailGroups(node, graph)`.
- [ ] Render grouped sections in `ReportingRail`.
- [ ] Run frontend tests.

### Task 3: Refine inspector metadata priority

**Goal:** Inspector labels and metadata remain concise and reporting-specific.

**Files:**
- Modify: `frontend/src/main.jsx`
- Test: `frontend/src/quality-dashboard.test.mjs`

**Acceptance Criteria:**
- [ ] Metadata includes clearer labels for template/source counts and reporting role/domain.
- [ ] Low-value technical fields remain present but capped by existing row limit.
- [ ] Useful links remain first.

**Verify:** `npm test` from `frontend` passes.

**Steps:**
- [ ] Add/adjust source-inspection test for metadata helpers.
- [ ] Refine label/order in `reportingMetadataRows`.
- [ ] Run frontend tests.
