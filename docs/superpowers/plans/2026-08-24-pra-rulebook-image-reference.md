# PRA Rulebook image-reference workspace implementation plan

> **For agentic workers:** Implement task-by-task with test-first changes and fresh verification after each task.

**Goal:** Recompose the PRA Rulebook interface at a 1280×853 desktop viewport so its shell, graph canvas and inspector match the supplied reference image while preserving existing data and interactions.

**Architecture:** Keep the existing React component tree, graph renderer and shared colour tokens. Add a small amount of semantic rail markup, change the desktop grid/default panel presentation, and place a single reference-aligned CSS layer at the end of the existing stylesheet so the current functional styles remain available to reporting, reading and responsive modes.

**Tech Stack:** React 19, Vite 8, react-force-graph-2d, CSS custom properties, Node `node:test`, Playwright smoke script.

**User decisions (already made):**

- “Make it look like this image.”
- Preserve the existing graph, reporting, reading-mode, filtering, feedback and source-link behaviour.
- Use the previously approved Phthalo Green / Mineral Aqua palette in the light-workspace composition, not the rejected full-dark or blue/sepia treatments.

---

## Files and responsibilities

- `frontend/src/imageReferenceLayout.test.mjs`: static regression checks for reference geometry, required rail markup and light-surface rules.
- `frontend/src/main.jsx`: add the rail brand/utility structure and make the inspector visible on desktop without changing API or graph data logic.
- `frontend/src/styles.css`: add the final reference-aligned geometry and surface rules, including graph labels, inspector cards, responsive thresholds and reporting/reader carry-through.
- `docs/superpowers/specs/2026-08-24-pra-rulebook-image-reference-design.md`: approved visual design and acceptance criteria.
- `outputs/pra-rulebook-image-reference/`: browser smoke script, logs and screenshots.

### Task 1: Add failing image-reference layout tests

**Goal:** Encode the measurable reference requirements before changing production code.

**Files:**

- Create: `frontend/src/imageReferenceLayout.test.mjs`
- Read: `frontend/src/main.jsx`
- Read: `frontend/src/styles.css`

**Acceptance Criteria:**

- [ ] The test checks the last `.shell` rule for `200px minmax(0,1fr) 370px` and a 58px top row.
- [ ] The test checks that the final rail, topbar, canvas and inspector rules use the approved dark/light surfaces.
- [ ] The test checks for `.rail-brand`, `.rail-brand-mark`, `.rail-collapse` and `.rail-utilities` in the JSX.
- [ ] The test checks that desktop initial panel logic uses a 900px threshold.

**Verify:** `npm test -- --test-name-pattern=image-reference` from `frontend` → FAIL because the current implementation has a 220px/320px grid and no new rail structure.

**Steps:**

1. Write the test with a `lastRuleBody` helper matching the existing colour-system test style.
2. Run the focused command and capture the expected assertion failure.
3. Do not change production files until the failure is observed.

### Task 2: Add semantic rail structure and desktop panel default

**Goal:** Make the DOM expose the same visual regions as the reference while retaining existing handlers and list rendering.

**Files:**

- Modify: `frontend/src/main.jsx` around `panelOpen` state and the `<aside className="rail">` block.
- Test: `frontend/src/imageReferenceLayout.test.mjs`

**Acceptance Criteria:**

- [ ] The rail has a brand header containing a shield-like inline mark, PRA Rulebook title/context and a collapse affordance.
- [ ] Existing Up one level, All Parts, error, filter and result controls remain present and use their existing handlers.
- [ ] The rail has a bottom utility strip with three non-data controls or links that do not interfere with result selection.
- [ ] Desktop widths above 900px initialise with the inspector open; mobile widths remain closed.

**Verify:** `npm test -- --test-name-pattern=image-reference` from `frontend` → PASS.

**Steps:**

1. Replace only the rail wrapper markup around the existing product/actions/result stack, preserving the mapped result buttons and callbacks.
2. Add `rail-brand`, `rail-brand-mark`, `rail-brand-copy`, `rail-collapse` and `rail-utilities` classes.
3. Change the panel initializer to `window.innerWidth > 900` and leave `choose(..., {openPanel:false})` unchanged if it is needed for bootstrap data loading; the CSS/test must reflect the intended desktop presentation after the initial selection path.
4. Run the focused test and the existing frontend suite.

### Task 3: Implement reference-aligned visual composition

**Goal:** Make graph, inspector, reporting and reader surfaces visually match the supplied reference at desktop size.

**Files:**

- Modify: `frontend/src/styles.css` after the current final override block.
- Modify: `frontend/src/main.jsx` only if a small class/attribute hook is needed by the CSS.
- Test: `frontend/src/imageReferenceLayout.test.mjs`

**Acceptance Criteria:**

- [ ] At 1280px width the shell uses 200px rail / flexible canvas / 370px inspector and a 58px topbar.
- [ ] The rail is deep green, the topbar and inspector are White, and the graph/reporting canvas is Mist.
- [ ] The central graph has a white rounded metadata card, white label pills, a compact white legend and white vertical zoom controls.
- [ ] The inspector has a white inset card, compact tabs, a Mist text well, Clear Azure underlined links, pale Aqua reading CTA and visible cross-reference content.
- [ ] Reporting and reading modes keep light White/Mist surfaces and do not inherit the old dark rail treatment into their content surfaces.
- [ ] Focus outlines remain 2px Mineral Aqua and responsive rules switch below 980px.

**Verify:** `npm run build` from `frontend` → exit 0, plus the browser smoke check in Task 4.

**Steps:**

1. Add a final CSS layer using the shared `--colour-*` tokens rather than new hex literals except for transparent shadows.
2. Set the desktop grid, inset surface margins, compact typography, rail header/footer, active row, graph canvas controls and inspector card geometry.
3. Override the existing reporting and reader surface rules at the same cascade level so they stay light.
4. Add the 980px and 560px responsive overrides.
5. Run the focused tests, all frontend tests and the production build.

### Task 4: Browser smoke check and evidence

**Goal:** Confirm the visual geometry and key surfaces in a real 1280×853 browser viewport.

**Files:**

- Create: `outputs/pra-rulebook-image-reference/check.py`
- Create: `outputs/pra-rulebook-image-reference/plan.md`
- Create: `outputs/pra-rulebook-image-reference/final_runs/run_1/screenshots/` PNG evidence
- Create: `outputs/pra-rulebook-image-reference/final_runs/run_1/final_script_log.txt`

**Acceptance Criteria:**

- [ ] The graph screenshot is captured at 1280×853 and its computed grid tracks are 200px / flexible / 370px, the inset inspector panel is 344px ± 4px, the topbar height is 58px and the canvas background is `rgb(244, 247, 245)`.
- [ ] The graph screenshot shows `.canvas-meta`, `.legend`, `.zoom` and `.inspector.open`.
- [ ] The reporting screenshot confirms White/Mist light surfaces.
- [ ] The reader screenshot confirms White reader and shelf surfaces.
- [ ] The log records the exact viewport, computed geometry and screenshot paths.

**Verify:** `/root/.openclaw/workspace/.venv-webwright/bin/python outputs/pra-rulebook-image-reference/check.py` with the frontend dev server running → exit 0 and three PNG screenshots.

**Steps:**

1. Start the existing Vite dev server on port 5173.
2. Use Playwright with viewport `{width: 1280, height: 853}` and the existing app URL.
3. Capture graph, reporting and reader states; assert the computed geometry and backgrounds before writing each screenshot.
4. Stop the dev server after the smoke check and inspect the screenshots with `view_image`.

### Final verification

Run from `frontend`:

```bash
npm test
npm run build
```

Then run the browser smoke check and inspect the three screenshots. Review `git diff --check` and `git status --short` before reporting the result.
