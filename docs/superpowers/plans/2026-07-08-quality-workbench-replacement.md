# Quality Workbench Replacement Implementation Plan

> **For agentic workers:** replace the current Quality tab, do not accrete new UI on top of it.

**Goal:** Replace the Quality tab with a small queue selector and a full-screen workflow surface for node feedback and unverifiable links.

**Architecture:** Keep existing backend endpoints. Replace the frontend quality dashboard component with a queue-first workbench. Move quality IA into small pure helpers that can be tested without browser automation.

**Tech Stack:** React, Vite, plain CSS, existing FastAPI endpoints.

**User decisions already made:**
- “Completely delete and replace the current quality workflow.”
- “Very small physically list of queues.”
- “Workflow should use most of the screen.”
- “I don’t like panels because waste space.”
- “Think Palantir.”

## Tasks

1. Add pure queue helpers and tests.
2. Replace `ValidationDashboard` and related quality workflow rendering with a queue workbench.
3. Replace the Quality CSS block with a denser monochrome workbench style.
4. Build, run focused tests, smoke the public app, commit and push.
