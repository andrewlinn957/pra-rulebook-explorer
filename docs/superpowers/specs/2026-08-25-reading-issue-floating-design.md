# Reading-mode floating issue report design

**Date:** 25 August 2026

## Goal

Make `Report an issue with this node` in reading mode a floating, non-modal editor that stays available while Andrew reads the provision and preserves an in-progress description when the editor is minimised or loses focus.

Graph and Reporting issue reports remain unchanged.

## User decisions

- The reader issue report is a floating window, not a reserved column in the reading layout.
- Its default position is near the top right of the reading view, below the reader header and clear of the pinned-reference shelf and provision text.
- Clicking elsewhere in the reader does not close the window and does not clear the draft.
- The description editor can be vertically resized.
- The description editor has a Minimise/Expand control. Minimising collapses the editor only; it does not unmount the report window or discard the draft.
- Close and Cancel remain explicit discard actions. Submit remains the explicit action that sends the report.
- At narrow widths, where a floating side position would obstruct reading or become unusably small, the report becomes a contained centred modal-style panel.

## Component and layout design

`IssueReportModal` will continue to own the shared report form and draft value, but reader context will select a separate presentation mode:

- Graph and Reporting use the existing `modal-backdrop` and close-on-background-click behaviour.
- Reading mode renders a reader issue layer inside the reader's containing canvas. The layer has no opaque page backdrop and does not reserve a grid track.
- The reader issue layer is non-interactive outside the form, allowing clicks to pass through to the provision and reference shelf. The form itself remains interactive.
- The floating form is positioned below the reader header and offset from the reference shelf using the reader's shelf-width layout variable. This keeps the default form in the upper-right margin without covering the main provision text.
- The existing node summary, source link, context note, Cancel, and Submit controls remain in the form.
- The form header adds a labelled Minimise/Expand button. When minimised, the node summary and description controls are hidden but the draft remains bound to the same React state and the form header/actions remain available.

The reader's normal two-column layout remains unchanged while the report is open. The reference shelf does not move to accommodate the report.

## Draft and interaction behaviour

The draft remains controlled by the existing `text`/`setText` props. Clicking or focusing any control outside the textarea does not reset it. The reader presentation must not use a backdrop click handler that calls `onClose`.

Minimising changes only local presentation state. Expanding restores the textarea with exactly the text entered before minimisation. The textarea uses vertical CSS resizing with a usable minimum height and a maximum height bounded by the floating form, so long reports remain scrollable rather than pushing the reader off-screen.

Explicit Close and Cancel actions call `onClose`, as they do today. Successful Submit follows the existing API path and clears the report window and draft after the server accepts the report. Failed submission leaves the window and draft intact and shows the existing error state.

## Responsive behaviour

- Desktop: the report layer is positioned below the reader header and to the left of the pinned-reference shelf clearance, with a bounded width suitable for the reading canvas.
- Intermediate widths: the report stays floating if the calculated reading margin can accommodate it; otherwise it uses the contained fallback.
- Narrow widths: the report is centred within the reader below its header, with a page-level dimming layer and a bounded height. The report remains a conventional focused panel at this breakpoint because allowing clicks through to the narrow reading surface would make the editor obscure too much content.

## Error handling and accessibility

- Reader mode uses `role="dialog"` without claiming a modal interaction when clicks through to the reader are enabled.
- Minimise/Expand has an explicit accessible label and state (`aria-expanded` or equivalent).
- The textarea remains labelled and retains its placeholder, max length, and controlled value.
- The existing saving, saved, and API error states remain visible and actionable.
- Background clicks in graph and Reporting modes retain their current close behaviour.

## Testing and acceptance criteria

Frontend tests will demonstrate that:

- the reader report is represented by a floating reader layer rather than the old three-column issue-report layout;
- the reader layer does not close from a background click, while graph/reporting still use the shared modal behaviour;
- the reader form exposes a Minimise/Expand control and keeps the textarea value controlled;
- the textarea is vertically resizable and has bounded responsive sizing;
- the reader layout retains its normal text/reference-shelf columns while the report is open;
- the explicit close, submit, saving, saved, and error paths remain wired.

The full frontend test suite, production build, and a targeted browser check will be run before completion. The browser check will open reading mode, open the report, verify its top-right floating geometry, type text, click elsewhere in the reader, minimise and expand the editor, and confirm the text remains.

## Out of scope

- Dragging or permanently repositioning the report window.
- Draft persistence after explicit Close or Cancel, page navigation, or reload.
- Changes to the graph/reporting report form or issue-log maintenance screen.
- Changes to the backend issue-report API.
