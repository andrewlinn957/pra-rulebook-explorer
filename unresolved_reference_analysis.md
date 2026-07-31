# Unresolved cross-reference analysis

Date: 2026-07-31

## Scope

This report analyses the 12,973 candidates in
`logs/reference-recall-corpus-review-final-20260731.sqlite3` that were classified
as genuine references but have no unique local target (`decision=REFERENCE`,
`target_status=external_or_unresolved`). This is a read-only analysis; it does
not change the Rulebook database.

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
