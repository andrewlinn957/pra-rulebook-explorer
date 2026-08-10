# Archived material

This directory contains historical material retained for provenance and possible
recovery. It is not part of the current PRA Rulebook Explorer application and
is not used by the application, tests or regular development commands.

## Contents

- `legacy-spec/rulebookexp.md` is the original June 2026 product specification.
  Its implementation status and corpus metrics are historical and should not be
  treated as current documentation.
- `legacy-tools/` contains one-off guidance-download, COR011 package and LLM
  reference-batch utilities from earlier ingestion and repair work. The current
  reporting graph pipeline uses the newer reporting package and reference-review
  tools in `scripts/`.

The files are preserved rather than deleted so their history and implementation
details remain available. They should not be run against the current database
without first checking their assumptions and input paths.
