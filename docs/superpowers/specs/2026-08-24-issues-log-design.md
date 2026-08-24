# PRA Rulebook issues log design

**Date:** 24 August 2026

## Goal

Add a compact, table-based Issues log to the PRA Rulebook Explorer. Existing reports continue to be created from the graph and reader views. The Issues log is a maintenance screen for reviewing, amending, and deleting those reports.

## User decisions

- The existing `Report an issue with this node` action in graph view and reader view remains the sole issue-creation workflow.
- The Issues log must not contain an Add issue button, node search, or manual issue form.
- Amending an issue changes its description and status only. The linked node and original report context remain fixed.
- Deleting an issue permanently removes it from the local issue log after an explicit confirmation.
- The log is opened from the existing bottom-left settings menu, not from the main Graph/Reporting navigation.
- The log should be compact and table based.

## Architecture

The existing JSONL queue under `outputs/reported-issues/reported-issues.jsonl` remains the source of truth. The existing `POST /issues/node` and `GET /issues` endpoints continue to support report creation and listing. The backend adds maintenance operations without changing the report payload or graph/reader workflow:

- `PATCH /issues/{issue_id}` updates `description` and `status`.
- `DELETE /issues/{issue_id}` permanently removes the matching item.

The repository layer validates the existing status set (`open`, `in_progress`, `resolved`, `wont_fix`) and the existing 2,000-character description limit. Successful amendments update `updated_at`; they never replace the stored node, `created_at`, `page_url`, or `context`. A missing issue is reported as a not-found API response. JSONL rewrites remain protected by the existing file-locking approach.

The React app gains an `issues` view state alongside `graph` and `reporting`. The settings popover contains the only Issues log entry point. The existing graph and reader report affordances remain unchanged and continue calling `POST /issues/node`.

## Issues log screen

The screen uses the existing PRA Rulebook visual system and a full-width light workspace while retaining the dark navigation rail. It contains:

- a compact heading and a `Back to explorer` action;
- summary counts for total, open, in progress, resolved, and won't fix reports;
- a status filter, including All;
- a dense table ordered newest first.

The table has columns for status, reported date, linked node, report description, context, and actions. The linked node cell shows the node type and title and exposes the original source URL when available. The node is display-only. The actions column contains Edit and Delete.

Edit opens a focused modal containing only a description textarea and status selector. Save sends the amended description and status, then refreshes the list and counts. Delete opens a confirmation prompt naming the linked node. Confirming sends the delete request, then refreshes the list and counts. Cancelling leaves the row unchanged.

The screen has explicit loading, empty, filtered-empty, and error states. Failed amendments or deletions leave the current row and list intact and expose the API error through the existing app error treatment. There is no add control anywhere in this screen.

## Data flow

1. Andrew reports an issue from a selected graph or reader node using the existing report modal.
2. `POST /issues/node` appends the report to the JSONL queue.
3. The settings menu opens the Issues view, which loads `GET /issues`.
4. The table filters the returned items locally while retaining the server-provided status counts.
5. Edit sends `PATCH /issues/{issue_id}` with only `description` and `status`.
6. Delete sends `DELETE /issues/{issue_id}` after confirmation.
7. Successful maintenance operations reload `GET /issues` so the table and counts reflect the queue.

## Error handling and validation

- Invalid JSON returns HTTP 400 with the existing JSON-body error format.
- An invalid status or oversized description returns HTTP 400 and leaves the queue unchanged.
- An unknown issue ID returns HTTP 404 and leaves the queue unchanged.
- A failed list, amendment, or deletion displays an error without removing or rewriting the visible row optimistically.
- The existing report endpoint remains covered so the feature cannot accidentally remove the graph/reader creation workflow.

## Testing and acceptance criteria

Backend tests must demonstrate that:

- existing creation persists the node, description, context, and timestamps;
- amendments change only description/status and `updated_at`;
- invalid statuses and oversized descriptions are rejected;
- deletion removes exactly the requested issue and missing IDs fail cleanly;
- GET, PATCH, DELETE, malformed JSON, and validation API paths return the expected responses;
- `POST /issues/node` still creates reports.

Frontend tests must demonstrate that:

- the Issues log is entered from the settings menu and is not exposed as a main Graph/Reporting navigation item;
- the Issues view is compact and table based;
- the table and edit form expose no Add issue action or node-editing control;
- the existing graph and reader report affordances remain present;
- loading, empty, filtered-empty, and error states are represented.

The frontend production build and the existing backend and frontend test suites must pass.

## Out of scope

- Creating issues from the Issues log.
- Changing the node attached to an existing issue.
- User accounts, permissions, assignment, comments, audit history, or external issue tracking.
- Replacing the JSONL queue with a database.
