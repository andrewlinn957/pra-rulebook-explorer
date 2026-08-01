# Reference-resolution policy audit

Generated 2026-08-01 after the policy reprocess and FCA Handbook repair.

The resolver now applies these scopes:

- **Provision:** a specific rule, article, section, paragraph, point, or named external provision is linked to a text-bearing node.
- **Document:** a general reference to a chapter, instrument, statute, guidance document, or template is linked to a source document URL.
- **Not reference:** extraction artefacts are retained in the ledger but do not create a graph target.

## Current ledger

| Outcome | Count |
| --- | ---: |
| Provision | 16,774 |
| Document | 18,610 |
| Not reference | 649 |
| Blank/unresolved targets | 0 |

## Contract checks

- Provision rows without source text: **0**
- Document rows without a URL: **0**
- Provision-fetch fallback links: **0**
- Materialised graph occurrences with an orphan source/target: **0**
- `PRAGMA foreign_key_check`: **0** violations

## Examples repaired

- FCA `SUP 10C.4.3R`, `SUP 16.12.11R`, `SUP TP 7.2.3R`, `GEN 4.4.1R`, and `COLL 8.2.6` now point to text extracted from the official FCA sourcebook PDFs.
- CRR `Article 128` and `Article 352` citations point to text-bearing official CRR Article nodes.
- `Condition A2(d)(ii)` in the Leverage Ratio (CRR) Part points to the text-bearing Article 429a node.
- Numbered internal cross-references such as Article 429b(4), Article 429(4), and rule 2.4 are now resolved to their text-bearing Rulebook nodes rather than being discarded as relative references.
- General relative references such as “Chapter 6 (Templates and Instructions) of this Part” now resolve to the containing source document URL.
- A bare `FSMA 2000` mention remains a document link to the Act, as required for a general instrument reference.

The generated FCA targets retain the official sourcebook PDF URL and their normalized sourcebook/provision path in node metadata.
