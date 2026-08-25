# Reading-mode floating issue report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the reading-mode node issue report into a floating, non-modal editor that stays clear of the reading layout and preserves its controlled draft while unfocused or minimised.

**Architecture:** Keep one shared `IssueReportModal` component and the existing graph/reporting modal path. Add a reader-specific render path inside the `.canvas`, where an absolute layer can float over the reader without adding a grid column. The reader path owns only presentation state for minimising, while the parent continues to own the description draft and API submission state.

**Tech Stack:** React 18 JSX, Vite, CSS media queries, Node's built-in test runner, and the existing Playwright-based browser smoke tooling.

**User decisions (already made):**

- The reader issue report is floating rather than a separate reading-layout panel.
- Its default position is toward the top right, clear of the provision text and pinned-reference shelf.
- Clicking outside the textarea leaves the window open and preserves the draft.
- The textarea can be vertically resized and the editor can be minimised without losing the draft.
- Graph and Reporting issue reports retain their existing modal behaviour.

---

## Files and responsibilities

- Modify `frontend/src/main.jsx:275-330` to render reader reports inside the canvas and graph/reporting reports through the existing outer modal path.
- Modify `frontend/src/main.jsx:450-464` to split reader and non-reader presentation, add controlled minimise state, and keep explicit Close/Cancel/Submit actions.
- Modify `frontend/src/styles.css:940-970,1316-1323,2115-2169` to expose the reader shelf width, position the floating layer, bound the editor, and provide the narrow-screen fallback.
- Modify `frontend/src/readingMode.test.mjs:625-633` to replace the old side-column assertions with regression coverage for the new markup and CSS contract.

## Implementation constraints

- Do not change the backend issue endpoint or payload.
- Do not change graph/reporting issue creation or the Issues log.
- Do not make the report draggable or persist drafts after explicit Close/Cancel, navigation, or reload.
- Follow the repository's existing source-inspection test style, then verify the actual interaction in a browser.

### Task 1: Reader report render path and draft-preserving controls

**Goal:** Render reader reports as a separate non-modal layer and add a local minimise state without changing the controlled `text`/`setText` contract.

**Files:**
- Modify: `frontend/src/main.jsx:275-330,450-464`
- Test: `frontend/src/readingMode.test.mjs:625-633`

**Acceptance Criteria:**
- [ ] `IssueReportModal` uses a `reading-issue-layer` wrapper with `role="dialog"` and `aria-modal="false"` for `reading_mode`.
- [ ] The reader wrapper has no backdrop `onClick` handler that calls `onClose`; graph/reporting continue to use the existing `modal-backdrop` close-on-background-click path.
- [ ] The reader report has `Minimise description` and `Expand description` controls with `aria-expanded`, and toggling them does not unmount the form or replace the `value={text}` binding.
- [ ] The reader report is rendered inside the `.canvas`, while the non-reader report remains rendered after the inspector, so the reader layer is positioned relative to the canvas.
- [ ] Explicit Close, Cancel, Submit, saving, saved, and error behaviour remains wired to the existing callbacks and state.

**Verify:** `cd frontend && node --test src/readingMode.test.mjs` → the new reader report test passes and the old three-column assertions are gone.

**Steps:**

- [ ] **Step 1: Replace the old layout assertions with failing interface tests.** In `frontend/src/readingMode.test.mjs`, replace the current test named `reading issue reports use a side column between the provision text and reference shelf` with assertions equivalent to:

```js
test('reading issue reports use a non-modal floating layer with a collapsible controlled draft', () => {
  const reportSource = interfaceSource.slice(
    interfaceSource.indexOf('function IssueReportModal'),
    interfaceSource.indexOf('function ProvisionReader'),
  );
  assert.match(reportSource, /reading-issue-layer/);
  assert.match(reportSource, /aria-modal="false"/);
  assert.match(reportSource, /Minimise description/);
  assert.match(reportSource, /Expand description/);
  assert.match(reportSource, /aria-expanded=\{!minimised\}/);
  assert.match(reportSource, /value=\{text\}/);
  assert.match(reportSource, /setText\(e\.target\.value\)/);
  assert.doesNotMatch(
    reportSource,
    /readingIssue[\s\S]*if\(e\.target===e\.currentTarget\)onClose\(\)/,
  );
});

test('reader issue reports render in the canvas while graph reports keep the outer path', () => {
  const canvasSource = interfaceSource.slice(
    interfaceSource.indexOf('<main className="canvas">'),
    interfaceSource.indexOf('<aside className={panelOpen'),
  );
  assert.match(canvasSource, /readingNode&&issueReportNode&&<IssueReportModal/);
  assert.match(interfaceSource, /issueReportNode&&!readingNode&&<IssueReportModal/);
});
```

- [ ] **Step 2: Run the focused test and confirm it fails for the missing reader layer.** Run `cd frontend && node --test src/readingMode.test.mjs`. The expected failure is an assertion that `reading-issue-layer` is absent or that the old layout still exists. Correct any test-regex mistakes before implementing.

- [ ] **Step 3: Move only the reader report render into the canvas.** Keep the existing reader/graph/reporting branches in the canvas, then add the reader report immediately after them:

```jsx
{readingNode && issueReportNode && <IssueReportModal
  node={issueReportNode}
  text={issueText}
  setText={setIssueText}
  saving={issueSaving}
  saved={issueSaved}
  context="reading_mode"
  onClose={() => setIssueReportNode(null)}
  onSubmit={submitIssueReport}
/>}
```

Keep the existing shared report render after the inspector, guarded with `issueReportNode && !readingNode`, and pass `context="graph_view"` there.

- [ ] **Step 4: Split `IssueReportModal` into reader and shared modal presentation.** Add `const [minimised,setMinimised]=useState(false);`. Build the report form once, with the reader-specific header control and body wrapper:

```jsx
const reportForm = <form
  className={`node-feedback-modal issue-report-modal ${readingIssue ? 'reading-issue-report-modal' : ''}`}
  role={readingIssue ? 'dialog' : undefined}
  aria-modal={readingIssue ? 'false' : undefined}
  aria-label="Report an issue with this node"
  onSubmit={event => { event.preventDefault(); onSubmit(); }}
>
  <div className="modal-head">
    <div><span className="eyebrow">Report an issue</span><h3>Report an issue with this node</h3></div>
    <div className="issue-report-head-actions">
      {readingIssue && <button
        type="button"
        className="issue-report-minimise"
        aria-expanded={!minimised}
        aria-label={minimised ? 'Expand description' : 'Minimise description'}
        onClick={() => setMinimised(current => !current)}
      >{minimised ? 'Expand description' : 'Minimise description'}</button>}
      <button type="button" onClick={onClose} aria-label="Close">×</button>
    </div>
  </div>
  <div className={`issue-report-body ${minimised ? 'is-minimised' : ''}`}>
    <div className="feedback-node-summary">
      <span>{label(node.node_type)}</span>
      <strong>{displayNodeTitle(node)}</strong>
      {node.url && <a href={node.url} target="_blank" rel="noopener noreferrer">Open source ↗</a>}
    </div>
    <label className={`feedback-editor ${readingIssue ? 'issue-report-description' : ''}`}>
      Describe the issue (optional)
      <textarea
        value={text}
        onChange={event => setText(event.target.value)}
        placeholder="Example: this node should link to SS3/18, but the reference is missing."
        autoFocus
      />
    </label>
    <p className="muted issue-context-note">
      {context === 'reading_mode' ? 'Reported from reading mode.' : 'Reported from graph view.'}
    </p>
  </div>
  <div className="modal-actions">
    <button type="button" onClick={onClose}>Cancel</button>
    <button type="submit" disabled={saving || saved} className={saved ? 'issue-saved' : ''}>
      {saved ? '✓ Reported' : saving ? 'Saving…' : 'Submit report'}
    </button>
  </div>
</form>;
```

Use the existing `modal-backdrop` wrapper only for non-reader reports. Return the reader form from `<div className="reading-issue-layer">{reportForm}</div>` with no wrapper click handler. Keep the textarea's existing `autoFocus` attribute; the controlled value remains mounted while the body is minimised.

- [ ] **Step 5: Run the focused test and then the full frontend suite.** Run `cd frontend && node --test src/readingMode.test.mjs`, followed by `cd frontend && npm test`. Both must exit 0 before moving to the CSS task.

- [ ] **Step 6: Commit the JSX and test change.** Run:

```bash
git add frontend/src/main.jsx frontend/src/readingMode.test.mjs
git commit -m "feat: add floating reader issue report state"
```

### Task 2: Floating geometry, resizing, and responsive fallback

**Goal:** Position the reader report near the upper-right of the reader without reserving a side column, bound its height and textarea, and provide a contained narrow-screen fallback.

**Files:**
- Modify: `frontend/src/styles.css:940-970,1316-1323,2115-2169`
- Test: `frontend/src/readingMode.test.mjs:625-670`

**Acceptance Criteria:**
- [ ] The normal reader layout remains a two-column provision/reference-shelf grid while a report is open; no issue-report grid track or reference-shelf grid-column override remains.
- [ ] The desktop layer is absolute, pointer-transparent outside the form, and places the form below the reader header with a right offset based on the shelf-width variable.
- [ ] The form remains interactive, has bounded overflow, and the textarea is vertically resizable with a bounded maximum height.
- [ ] Minimise hides the summary/editor body without clearing its controlled value; Expand restores it.
- [ ] At widths below 1100px the layer becomes a contained centred panel with a dimmed surface, and at widths below 860px it remains bounded below the compact reader header.

**Verify:** `cd frontend && node --test src/readingMode.test.mjs && npm run build` → focused tests pass and Vite reports a successful production build.

**Steps:**

- [ ] **Step 1: Add failing CSS contract assertions.** Extend the reader issue test with assertions equivalent to:

```js
assert.match(interfaceStyles, /\.provision-reader\{[^}]*--reader-shelf-width/);
assert.match(interfaceStyles, /\.provision-reader-layout\{[^}]*var\(--reader-shelf-width\)/);
assert.match(interfaceStyles, /\.reading-issue-layer\{[^}]*position:absolute/);
assert.match(interfaceStyles, /\.reading-issue-layer\{[^}]*pointer-events:none/);
assert.match(interfaceStyles, /\.reading-issue-layer \.reading-issue-report-modal\{[^}]*pointer-events:auto/);
assert.match(interfaceStyles, /right:calc\(var\(--reader-shelf-width\) \+ 18px\)/);
assert.match(interfaceStyles, /\.issue-report-body\.is-minimised[^{]*\{[^}]*display:none/);
assert.match(interfaceStyles, /\.issue-report-description textarea[^}]*resize:vertical/);
assert.doesNotMatch(interfaceStyles, /\.shell\.reading-issue-open \.provision-reader-layout\{[^}]*minmax\(280px,320px\)/);
assert.doesNotMatch(interfaceStyles, /\.shell\.reading-issue-open \.reference-shelf\{[^}]*grid-column:3/);
assert.match(interfaceStyles, /@media\(max-width:1100px\)[\s\S]*\.reading-issue-layer/);
```

- [ ] **Step 2: Run the focused test and confirm it fails because the new layer rules are absent.** Run `cd frontend && node --test src/readingMode.test.mjs`. The failure must identify a missing CSS contract, not a syntax error.

- [ ] **Step 3: Replace the old issue grid rules with reader-layer rules.** Keep the shared base `.modal-backdrop` styles. Add the shelf variable to the reader and replace the old `reading-issue-backdrop` block with:

```css
.provision-reader{
  --reader-shelf-width:clamp(290px,27vw,350px);
}
.provision-reader-layout{
  grid-template-columns:minmax(0,1fr) var(--reader-shelf-width);
}
.reading-issue-layer{
  position:absolute;
  inset:0;
  z-index:20;
  pointer-events:none;
}
.reading-issue-layer .reading-issue-report-modal{
  position:absolute;
  top:calc(58px + 18px);
  right:calc(var(--reader-shelf-width) + 18px);
  width:min(340px,calc(100% - var(--reader-shelf-width) - 42px));
  max-height:calc(100% - 94px);
  margin:0;
  overflow:auto;
  pointer-events:auto;
}
.issue-report-head-actions{display:flex;align-items:center;gap:8px;margin-left:auto}
.issue-report-minimise{border:1px solid var(--colour-border);border-radius:7px;background:var(--colour-page);color:var(--colour-brand);padding:5px 8px;font-size:10px;font-weight:750}
.issue-report-minimise:hover{border-color:var(--colour-accent);background:var(--colour-accent-soft)}
.issue-report-body{display:grid;gap:14px;min-height:0}
.issue-report-body.is-minimised{display:none}
.issue-report-description textarea{min-height:110px;max-height:42vh;resize:vertical;overflow:auto}
```

Remove the old `.shell.reading-issue-open .provision-reader-layout`, `.shell.reading-issue-open .reference-shelf`, and grid-based `.reading-issue-backdrop` rules so the reader's normal grid remains intact.

- [ ] **Step 4: Add the contained fallback without restoring the side column.** Add media rules that keep the layer in the canvas and prevent background clicks from closing it:

```css
@media(max-width:1100px){
  .reading-issue-layer{
    inset:54px 0 0;
    display:grid;
    place-items:center;
    padding:18px;
    background:rgba(18,53,36,.22);
    pointer-events:auto;
    overflow:auto;
  }
  .reading-issue-layer .reading-issue-report-modal{
    position:relative;
    top:auto;
    right:auto;
    width:min(620px,100%);
    max-height:calc(100% - 36px);
  }
}
@media(max-width:860px){
  .reading-issue-layer{inset:54px 0 0;padding:12px}
  .reading-issue-layer .reading-issue-report-modal{max-height:calc(100% - 24px)}
}
```

- [ ] **Step 5: Run the focused tests, full frontend tests, and build.** Run:

```bash
cd frontend
node --test src/readingMode.test.mjs
npm test
npm run build
```

Expected output: all Node tests pass, and `vite build` completes with exit code 0.

- [ ] **Step 6: Commit the CSS and regression coverage.** Run:

```bash
git add frontend/src/styles.css frontend/src/readingMode.test.mjs
git commit -m "feat: float reader issue report beside reading surface"
```

### Task 3: Browser interaction and geometry verification

**Goal:** Confirm the implemented report behaves correctly in a real reader view at desktop and narrow widths.

**Files:**
- Create: `outputs/pra-rulebook-floating-issue-report/check.py`
- Create: `outputs/pra-rulebook-floating-issue-report/` screenshots and action log

**Acceptance Criteria:**
- [ ] A desktop reading view opens with the report form near the upper-right, below the reader header, without adding a third reader-layout column.
- [ ] Typing a non-empty draft, clicking the provision outside the textarea, minimising, and expanding leaves the exact draft unchanged.
- [ ] The desktop report layer has `pointer-events:none` outside the form and the textarea reports vertical resize capability through computed CSS.
- [ ] The narrow viewport shows a contained centred report panel and the same draft-preservation interaction.
- [ ] No browser console page error occurs during the scripted flow.

**Verify:** Run the saved Playwright check with `/root/.openclaw/workspace/.venv-webwright/bin/python outputs/pra-rulebook-floating-issue-report/check.py`; inspect its screenshots and action log, then run `git diff --check`.

**Steps:**

- [ ] **Step 1: Start the frontend and API using the repository's existing launch scripts.** Run `scripts/run_api.sh` and `scripts/run_frontend.sh` in the established local environment, recording their PIDs or use the existing test harness if the services are already running.

- [ ] **Step 2: Write the browser check.** Use Playwright to navigate to the local PRA Rulebook app, select a node, enter Reading mode, click `Report an issue with this node`, and assert:

```python
await page.get_by_role("dialog", name="Report an issue with this node").wait_for()
await page.locator(".reading-issue-layer").wait_for()
await page.locator("textarea").fill("Draft survives leaving the editor")
await page.locator(".provision-reading-spine").click(position={"x": 20, "y": 20})
assert await page.locator("textarea").input_value() == "Draft survives leaving the editor"
await page.get_by_role("button", name="Minimise description").click()
assert await page.get_by_role("button", name="Expand description").get_attribute("aria-expanded") == "false"
await page.get_by_role("button", name="Expand description").click()
assert await page.locator("textarea").input_value() == "Draft survives leaving the editor"
```

Capture desktop and narrow screenshots after opening and after expanding the preserved draft. Record browser console errors and the measured bounding boxes for the report, reader header, provision spine, and reference shelf.

- [ ] **Step 3: Run the browser check and inspect evidence.** Confirm the desktop panel's top edge is below the header, its right edge respects the shelf clearance, and its footprint does not alter the reader grid. Confirm the narrow panel is contained and centred. Any geometry failure is fixed in the CSS, then the browser check is rerun.

- [ ] **Step 4: Run final verification.** Run `cd frontend && npm test && npm run build` and `git diff --check`. Confirm `git status --short` contains only the intended app commits or unrelated pre-existing workspace state, and report the exact test/build/browser evidence.

## Plan self-review

- Spec coverage: render path, non-modal semantics, draft preservation, minimise/expand, textarea resizing, desktop shelf clearance, narrow fallback, graph/reporting non-regression, accessibility, tests, build, and browser evidence are each assigned to Tasks 1-3.
- Placeholder scan: no unresolved task, command, path, or acceptance criterion is left for the implementer to infer.
- Type/selector consistency: `reading-issue-layer`, `reading-issue-report-modal`, `issue-report-body`, `issue-report-description`, `minimised`, and `reader-shelf-width` are used consistently across the JSX, CSS, and tests.
