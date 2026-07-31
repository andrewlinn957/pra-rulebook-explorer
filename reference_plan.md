# Reference recall plan

## Objective

Identify and safely materialise cross-references that are still missing from the PRA Rulebook Explorer, while distinguishing genuine omissions from headings, boilerplate, self-references and references that cannot be resolved to a canonical target.

## Current evidence

- The corpus contains approximately 49,015 graph nodes.
- 35,553 nodes have already been processed by the existing LLM extractor.
- That pass produced 36,033 extracted reference rows: 14,340 resolved to targets and 21,693 unresolved.
- The existing extractor sends only the first 6,000 characters of long provisions. There are 434 long nodes, and a simple lexical scan finds citation-like terms after that cutoff in 406 of them.
- The latest resolution table contains 3,687 resolved references that are not represented by an existing edge. 2,406 pass the current confidence thresholds; 1,281 were held back by those thresholds and need review rather than automatic discard.

These figures are an audit baseline, not a claim that every unresolved row is a missing reference. Many unresolved rows refer to external instruments or contain ambiguous wording.

## Phase 1: build a reference-gap ledger

Create a unified, read-only ledger for every source provision. Compare:

- explicit HTML links and anchors;
- deterministic Article, section, regulation, paragraph, Annex, Chapter, Part, template and named-document detectors;
- existing `reference_occurrence` rows;
- existing graph edges; and
- previous LLM extraction and resolution findings.

Each candidate should retain its source node, exact character span, surrounding text, source context and reason for review. Filter navigation, repeated headings, boilerplate and self-references before ranking candidates.

The ledger should be resumable and hash each source text so that unchanged provisions are not reprocessed.

## Phase 2: repair deterministic gaps first

Recover references without model judgment wherever the source is sufficiently explicit:

- PRA HTML links and internal anchors;
- internal Article, paragraph, Annex, Chapter and Part references;
- named Parts, templates, tables, forms and guidance documents;
- statutory and regulatory citations covered by the legal-instrument registry; and
- citations occurring beyond the existing 6,000-character LLM limit.

This phase should update only staged occurrence/edge output and must not overwrite valid existing relationships.

## Phase 3: run batched LLM recall reviews

Use the existing OpenAI/OpenClaw integration as a read-only reviewer. Submit batches of independent provision or text-chunk items; keep each item independent so references cannot leak between provisions.

For long provisions, send overlapping chunks with absolute source offsets. Each item should include:

- source node ID and source context;
- the complete relevant text chunk;
- already detected references, when useful for finding omissions; and
- the required structured output schema.

Each reviewer finding must contain:

```json
{
  "source_node_id": "...",
  "span_start": 123,
  "span_end": 164,
  "quoted_text": "...",
  "target_hint": "...",
  "target_kind": "rule|part|article|annex|statute|external",
  "decision": "REFERENCE|NOT_REFERENCE|AMBIGUOUS",
  "confidence": 0.0
}
```

Run batches in this order:

1. Long-provision tails.
2. Resolved-but-unrepresented references.
3. High-confidence unresolved LLM findings.
4. Lexical candidates with no detected reference surface.
5. A stratified negative sample to estimate false-negative rates.

Reviewer agents must produce JSONL results only. They must not write graph edges directly.

## Phase 4: resolve and adjudicate

Resolve model findings against the local node corpus and legal-instrument registry. Automatically accept only when:

- the quoted text is an exact substring of the source;
- the target is uniquely resolved;
- the finding is not a self-reference or duplicate; and
- the extracted and resolver confidence meet the agreed threshold.

Send ambiguous findings, low-confidence matches and competing target candidates to a second adjudicator model. Preserve the candidate set and evidence used by the adjudicator.

## Phase 5: apply and validate

Materialise accepted findings as versioned `reference_occurrence` rows and graph edges with:

- model and prompt version;
- batch ID;
- source text hash;
- exact source span and quote;
- resolver evidence; and
- adjudication status.

Validation gates:

- every explicit HTML link is represented or explicitly classified as navigation/boilerplate;
- every accepted finding has a valid source span;
- no duplicate or self-reference edges are introduced;
- coverage is reported by Part, source type and reference category;
- a blind sample is reviewed for recall and false positives; and
- the reader displays newly recovered references and nested references correctly.

## First milestone

Implement a dry-run `reference-recall-audit` pipeline and produce the gap ledger. Then run a pilot over approximately 200 high-priority provisions before processing the full queue. Review the pilot metrics and adjudication quality before applying any new edges corpus-wide.

## Implementation status (2026-07-31)

The first milestone is implemented as a read-only workflow:

- `scripts/reference_recall_audit.py` scans the source node types, exact legal-citation groups, lexical structure/named-document candidates, HTML reference edges, `reference_occurrence` rows, graph edges and prior LLM findings into a separate SQLite ledger. Each source is hashed; `--resume` skips an unchanged source and long text is split into overlapping chunks with absolute offsets.
- `scripts/reference_recall_batches.py` turns a pilot JSONL into independent reviewer requests. It requires the reviewer to return exact quoted text, absolute spans, a target hint/kind (including forms and directives), `REFERENCE`/`NOT_REFERENCE`/`AMBIGUOUS`, and numeric confidence. It does not submit requests or write graph data.
- `scripts/validate_reference_recall_reviews.py` validates direct or OpenAI Batch JSONL responses against the pilot. It rejects missing source identity, non-exact quotes, invalid absolute spans and invalid decisions before any resolver sees a finding; `--allow-partial` supports independently reviewed sub-batches without treating the intentionally unreviewed remainder as a validation failure.
- The full dry run used the current corpus and produced `logs/reference-recall-ledger-20260731.sqlite3`. It scanned 29,693 eligible source provisions and recorded 106,558 candidates: 54,666 already covered by an edge, 5,818 by an occurrence, 839 by a prior LLM result, 4,877 unresolved prior LLM findings, 8,028 deterministic candidates needing review, and 23,333 candidates in text beyond the old 6,000-character LLM prefix. It also produced 2,974 overlapping chunks.
- The first pilot contains 200 distinct provision/chunk items in `logs/reference-recall-pilot-20260731.jsonl`; the first 50 read-only reviewer requests are in `logs/reference-recall-review-batches-20260731/`.

No occurrence rows or graph edges were changed by this milestone. The next gate is to review/adjudicate the pilot output, then run deterministic repairs and only apply findings whose exact span, target resolution and duplicate/self-reference checks pass.

## Implementation status (continued, 2026-07-31)

The review and materialisation gates are now implemented and have been run against the current corpus:

- `scripts/reference_recall_stage.py` stages deterministic repairs and previously resolved LLM proposals in a separate SQLite database. It re-checks the live source text, keeps exact spans, rejects self-references and existing relationships, and labels glossary targets as `DEF`/`uses_defined_term` while ordinary targets remain `REF`/`references`. LLM evidence sentences are retained as provenance, but the clickable quote is narrowed to the target identifier when possible.
- `scripts/import_reference_recall_reviews.py` imports complete or partial reviewer JSONL into a separate review database. It validates source identity, live text hashes and absolute spans, records negative/ambiguous/unresolved findings, and only marks a finding `eligible_reviewed` when local resolution and confidence/duplicate gates pass. The first 75 pilot rows have now been independently reviewed in batches. Rows 1–35 contain 332 exact findings (48 duplicate relationships, two duplicate occurrences, seven ambiguous targets, 268 unresolved targets and seven eligible reviewed findings); rows 36–75 add another 212 exact findings, all held by duplicate, ambiguity or unresolved-target gates. The seven eligible findings include the unique `FCA Handbook` finding and six later findings promoted through the guarded stage. No unresolved reviewer finding was guessed into the graph.
- `scripts/apply_reference_recall_stage.py` is dry-run by default and requires `--apply` for writes. It rechecks the source hash, exact quote, target existence, self-reference and duplicate occurrence/edge constraints inside one transaction, then writes versioned edge/occurrence provenance. A backup was taken before applying the gate-passing stage.
- The refreshed stage contained 1,140 eligible occurrences representing 1,011 unique source-target relationships; the separately reviewed promotions account for seven reviewed findings. Materialisation now contains 980 `references` edges and 31 `uses_defined_term` edges, with 1,109 `REF` and 31 `DEF` occurrences from this recovery method. Post-write checks found zero self-edges, zero missing edge targets, zero occurrences pointing to missing edges, zero duplicate source-target-span occurrences, and zero non-exact citation spans.
- Scanner version 2 now excludes generic/table instrument fragments such as standalone `Rules`, `Code` and `Regulations` from the substantive review queue while retaining specific instruments with years, numbers or distinctive titles. The final ledger (`logs/reference-recall-ledger-final-v2-20260731.sqlite3`) scanned all 29,693 source provisions and reports 53,088 candidates covered by an edge, 6,671 by an occurrence, 6,695 deterministic candidates needing review, 4,269 prior LLM findings still unresolved, and 22,892 tail candidates queued for batch review. SQLite `quick_check` returned `ok` and `foreign_key_check` returned no violations.
- `backend/app/graph.py` now treats `reference_recall_stage_v1` as an explicit evidence method, so the recovered relationships are visible in explicit graph/reader requests as well as ordinary graph requests.
- Reviewer outputs for rows 1–75 are retained under `logs/reference-recall-review-outputs-20260731/`; the full 200-item pilot remains independently batchable. Unresolved reviewer targets and the held ledger queue remain staged rather than being silently converted into edges. The next operational step is to continue the remaining pilot batches, then send ambiguous or competing local resolutions to a second adjudicator before any further promotion.
