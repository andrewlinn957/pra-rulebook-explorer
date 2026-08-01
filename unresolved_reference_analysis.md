# Unresolved cross-reference analysis

Date: 2026-07-31 (updated after the recommended-order implementation)

## Final follow-through snapshot (2026-08-01)

The full follow-through was rerun against
`backend/data/rulebook.sqlite3` and the authoritative ledger
`logs/reference-recall-ledger-recommended-final5-20260801.sqlite3` (106,690
candidates across 29,693 source nodes). The final corpus review is
`logs/reference-recall-corpus-review-final5-20260801.sqlite3`.

### Applied recovery

- 36 registry-backed non-CRR official target nodes were fetched and inserted;
  39 exact legal occurrences were staged and materialised. The 100 fetch
  failures are recorded separately as stale/nonexistent official paths (for
  example CRD IV Articles 325–377 cited by SS13/13, amended FSMA/Solvency II
  paths, and historical Companies/Building Societies Act paths); no target was
  invented for a failed fetch.
- The authoritative scanner now repairs malformed structural spans before
  candidate IDs are assigned, preserving the original span in detector
  evidence. The focused span tests pass.
- Parent Part/document fallback resolved 791 previously context-free
  structural occurrences (486 new relationships); 9 self-references and 7,280
  non-unique structural cases remain held.
- Exact defined-term aliases added 726 occurrences (72 new relationships).
  Generic labels without an exact document/defined-term node are explicit
  external holds: 935 labels (including PRA Rulebook, EBA Guidelines and the
  Financial Services and Markets Act 2000) were not linked to arbitrary
  provisions.

### Final review outcomes

| Review outcome | Count |
| --- | ---: |
| Covered | 78,575 |
| External/unresolved genuine references | 5,317 |
| Competing local targets | 1,933 |
| Structural context still required | 8,408 |
| Unsupported candidate kind | 77 |
| Not a reference / excluded context | 12,378 |

The 1,933 competing cases are isolated in
`logs/reference-recall-adjudication-queue-final5-20260801.jsonl`, with exact
source hashes/spans, context windows and all candidate target metadata. 77
have a unique context-only recommendation; 1,856 require human/LLM
confirmation. The queue is advisory and does not write graph edges.

### Integrity snapshot

The database has zero self-edges, zero edges with missing sources or targets,
and zero materialised occurrences with missing sources or targets. The only
duplicate source/target/span remains the pre-existing pair recorded below;
none was introduced by the new passes. The final SQLite `quick_check` and
foreign-key check are run as part of the release verification.

The remaining unresolved counts are therefore classified rather than an
unbounded extraction tail: official-source failures, absent external
documents, structural context holds, and an explicit competing-target queue.

## Historical recommended-order implementation status (2026-07-31)

The earlier sections below are the initial pattern analysis. The following
results are the authoritative post-implementation snapshot from
`logs/reference-recall-corpus-review-recommended-final3-20260731.sqlite3` and
`backend/data/rulebook.sqlite3`.

### What was applied

1. **CRR and exact local article targets.** The existing CRR pass added 161
   missing relationships. The occurrence bridge added 79 exact CRR spans. The
   aggregate legal pass then learned to map qualified CRR citations (for
   example, `Article 277(3)` and `Article 277a(1)`) to the existing Rulebook
   Article nodes instead of inventing missing external IDs.
2. **Guidance-code and defined-term aliases.** 387 unique PRA document-code
   proposals and 1,173 exact defined-term proposals were staged; unresolved,
   duplicate-code and self-reference cases were held rather than guessed.
3. **Legal-registry recovery.** The atomic legal pass materialised 8,046
   genuine citation occurrences with zero unresolved fetch failures. A bounded
   aggregate pass materialised a further 3,722 exact occurrences (2,650 in
   the first pass and 1,072 after local CRR article mapping), reusing existing
   relationships where necessary. The resolver uses short candidate snippets
   so large Parts do not trigger a quadratic full-document scan.
4. **Structural/contextual recovery.** 1,639 exact structural occurrences
   were materialised (798 new relationships); ambiguous, duplicate and
   self-reference candidates remain held. The structural stage also excluded
   3,347 boilerplate-like candidates from automatic linking.

### Final corpus counts

Before this work the authoritative review had 70,993 covered candidates,
10,532 external/unresolved references and 12,676 ambiguous candidates. After
the stages above it reports:

| Review outcome | Count |
| --- | ---: |
| Covered | 76,954 |
| External/unresolved genuine references | 6,203 |
| Ambiguous (multiple local targets) | 1,951 |
| Ambiguous (structural context required) | 9,018 |
| Ambiguous (unsupported candidate kind) | 77 |
| Not a reference / excluded context | 12,355 |

The remaining 6,203 genuine references are:

| Candidate type | Count | Main source types |
| --- | ---: | --- |
| Legal citation | 2,962 | chapter 1,651; part 952; guidance document 332; guidance section 27 |
| Named instrument | 1,242 | definitions and guidance paragraphs dominate |
| Named document | 1,169 | guidance paragraphs/documents dominate |
| Structure reference | 830 | guidance documents and paragraphs dominate |

### Patterns in the remaining holds

- **Legal citations:** the residuals are now concentrated in aggregate Parts,
  Chapters and guidance documents. Common phrases include `Article 4(2)`,
  `Article 13(2)`, `Articles 32 to 35`, `Article 117(2)`, `Article 118`, and
  `Article 428f`. The remaining causes are primarily missing official target
  nodes for non-CRR instruments, bare citations whose instrument is not
  recoverable from the short local context, and malformed long spans.
- **Named documents:** the largest exact labels are `the PRA Rulebook` (511),
  `the Financial Services and Markets Act 2000` (68), `PRA Rulebook Rules`
  (29), `EBA Guidelines` (26), and missing/old supervisory codes such as
  `SS29/19`, `CP16/14`, `PS3/24` and `PS15/24`. These should remain document-
  level or external holds unless an exact local document node exists.
- **Named instruments:** the most frequent truncated or generic labels are
  `EU Guidelines` (47), `Companies Act` (45), `Fundamental Rule` (47),
  `EU Exit) Regulations` (36), `Council Directive` (34), `Banking Act` (33),
  `Commission Delegated Regulation` (25), and `Interpretation Act` (22).
  A year, number, jurisdiction or nearby defined term is required before
  selecting a target.
- **Structure references:** 830 remain, with recurring malformed spans such
  as `Table 2 in 9`, `paragraph 9 of Part II of`, and concatenated PRA Rules,
  EBA Guidelines and annex/table references. These are detector-span problems
  rather than safe unique-target matches.

### Integrity and verification

The post-apply database passes `PRAGMA quick_check` (`ok`) and
`PRAGMA foreign_key_check` (no rows). There are zero self-edges, zero edges
with missing targets, and zero materialised occurrences with missing targets.
One duplicate source/target/span remains from the pre-existing
`legal_reference_occurrence_v1` layer (`025267ac6855b951` →
`acda4f3ef5f1bf19`, span 146–152); no new duplicate was introduced by these
stages.

### Next work order

1. Add a cached, batch official-source materialiser for the remaining
   non-CRR registry targets, reporting fetch failures separately from detector
   ambiguity.
2. Repair long/truncated candidate spans at punctuation and instrument
   boundaries, then rerun the bounded legal resolver.
3. Resolve structural references against parent Part/Chapter/document
   metadata, preserving only unique matches.
4. Treat generic document labels as document-level/defined-term references,
   not arbitrary provision links; keep absent PRA/EBA documents explicitly
   external.
5. Send only the residual competing-target cases to human/LLM adjudication.

## Scope

This report analyses the 12,973 candidates in
`logs/reference-recall-corpus-review-final-20260731.sqlite3` that were classified
as genuine references but have no unique local target (`decision=REFERENCE`,
`target_status=external_or_unresolved`). That historical snapshot was
read-only; the implementation status above records the subsequent
evidence-checked materialisation work.

The set is distributed across these candidate types:

| Candidate type | Count |
| --- | ---: |
| Legal citation | 7,361 |
| Named document | 2,800 |
| Named instrument | 1,812 |
| Structure reference | 999 |
| Article citation | 1 |

The source context is concentrated in `part` (4,195) and `chapter` (3,223)
nodes, followed by `guidance_document` (2,763), `guidance_paragraph` (1,768),
`defined_term` (538), `guidance_section` (390) and `rule` (96).

## Resolution patterns

### 1. CRR article references are the largest high-confidence opportunity

There are 3,111 candidate rows in CRR-like source titles, containing 3,216
Article mentions. A base UK-CRR article node exists for 2,756 of those mentions.
Many are currently held only because the corpus pass deliberately did not run
the expensive legal registry matcher over the entire tail.

Recommended treatment:

- use the source title and nearby text to distinguish CRR Articles from other
  instruments cited in the same provision;
- resolve the article number to `external:uk-crr:article:<number>`;
- preserve subsection text as occurrence metadata, falling back to the base
  article when a subsection node is not present; and
- preserve duplicate and self-reference checks before materialisation.

### 2. PRA document codes can use canonical code aliases

There are 804 `SS/PS/FG/CP` code mentions across 493 candidate rows. A simple
prefix lookup finds a unique local `guidance_document` for 494 mentions. The
remaining 310 mentions have no matching local document title and should be
treated as external or missing-document cases rather than guessed.

Recommended treatment:

- index canonical guidance documents by normalized code (`SS24/15`, `PS3/24`,
  etc.);
- accept code-prefix matches even when the candidate contains only the code;
- map to the document node, not one of its paragraph children; and
- maintain an explicit external-document hold for codes absent from the local
  corpus.

### 3. Legal citations need full-provision context

The legal-citation set contains 7,361 rows. Many candidate phrases are bare
Articles, sections or regulations, while the instrument name occurs elsewhere
in the provision. A candidate-only lookup therefore cannot reliably identify
the target instrument.

Useful signals include:

- CRR/UK CRR source titles and nearby CRR references;
- explicit FSMA/Financial Services and Markets Act wording;
- EU or SI numbers, such as `Regulation (EU) No ...` or `S.I. 20../...`;
- named Acts, Regulations and Orders in the surrounding sentence; and
- existing registry aliases and instrument IDs.

The existing database also shows a node-coverage gap for some otherwise known
legal targets, including UK CRR Article 109 and several Article 1/Article 18
subsections. Those should be added or linked to their base provision before
expecting every registry match to materialise.

### 4. Structural references are often locally resolvable with context

There are 999 structure references. 852 unresolved candidates contain an anchor
such as `of Part`, `of Annex`, `of Schedule`, `of Chapter`, `of this SS` or `of
the PRA Rulebook`. These are strong signals that the source document’s parent
part/chapter or guidance document can disambiguate the target.

Repeated examples include:

- `paragraph ... of Part ...`;
- `paragraph ... of Schedule ...`;
- `Table ...`, `Form ...` and `template ...` references; and
- references such as `paragraph 2.11 of this SS`.

Recommended treatment is a context-aware resolver using the source node’s
document, part, chapter and guidance-code metadata before considering external
targets.

### 5. Generic document labels need semantic handling

The most common exact labels include:

- `CRR` / `the CRR`: 859 candidates;
- `the PRA Rulebook`: 822 candidates;
- `Solvency II Directive`: 249 candidates;
- `PRA Handbook`: 36 candidates; and
- `Capital Requirements Regulation` variants: 52 candidates.

Several of these already have defined-term nodes. They should normally become
`DEF`/defined-term relationships or document-level links, not arbitrary links
to a part or article. The PRA Rulebook itself has no single canonical provision
node, so it should remain a document-level reference unless a specific part is
named.

### 6. Named-instrument matching is affected by truncation and ambiguity

The 1,812 named-instrument rows include labels such as `Companies Act`,
`Banking Act`, `Solvency II Directive`, `EU Exit) Regulations` and
`Commission Delegated Regulation`. A simple title-containment probe finds a
possible local-title overlap for 1,233 rows, but 1,011 of those have multiple
possible matches. The label alone is therefore insufficient; the surrounding
year, number, jurisdiction and provision type must be used.

## Detector-quality findings

The unresolved set also contains evidence that candidate extraction should be
improved before another materialisation pass:

- 1,058 candidate phrases are longer than 100 characters;
- 547 have unbalanced parentheses;
- 279 end in a connector such as `of`, `the`, `and` or `to`;
- 2,109 candidate texts differ from the live span, but the difference is
  whitespace compaction rather than different wording.

The legal and structure detectors should keep a short clickable citation span
and store the longer sentence as evidence. Trimming at punctuation, closing
parentheses and known instrument boundaries will reduce false ambiguity and
improve registry matching.

## Recommended work order

1. Resolve CRR article mentions using source-title/context rules and existing
   UK-CRR article nodes.
2. Add normalized aliases for PRA/supervisory-document codes.
3. Run the legal-instrument registry only on legal candidates with a plausible
   instrument signal, using the full source provision for context.
4. Resolve Part/Chapter/Annex/Schedule/Table/Form references against the source
   document hierarchy.
5. Map generic CRR, Solvency II and Handbook labels to defined terms or
   document-level targets.
6. Repair candidate-span extraction and rerun the resolver.
7. Send only the residual competing-target cases to adjudication.

The 12,973 rows are explicit, auditable holds rather than an unreviewed tail.
The newly materialised recovery rows have valid source and target nodes; this
report concerns the remaining resolution opportunities.
