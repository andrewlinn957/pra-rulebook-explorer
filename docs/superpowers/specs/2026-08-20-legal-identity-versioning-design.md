# Legal identity and provision versioning design

## Scope

Separate the stable legal identity of a PRA Rulebook provision from the dated
version of its text and the source page or fetched snapshot that supplied that
version.

The change covers PRA Rulebook `rule` nodes and their graph relationships. It
does not change the identity model for guidance paragraphs, reporting-estate
nodes, glossary terms or external legislation in this pass.

## Current problem

The parser builds a Part key from the complete dated URL, for example
`part:pra-rules/liquidity-coverage-ratio-crr/01-06-2026`. Every Chapter and Rule
key is derived from that Part key. A later dated page therefore creates a new
node identity even when it contains the same legal provision. The current
`document_source` table also updates one mutable row per URL and cannot retain
multiple immutable fetches of the same page.

## Data model

The existing graph tables remain the serving model, with two new explicit
layers and an immutable snapshot store:

```text
provision                         canonical legal identity
  └─ has_version → rule            dated provision version, text-bearing
                                      └─ sourced_from → part
                                                     dated source page
                                      document_snapshot preserves each fetch
```

### Canonical provision

Create a `node_type='provision'` node with a date-free stable key:

```text
provision:pra-rules/<part-path>:<structural-locator>:<rule-number>
```

The structural locator preserves the existing Chapter/HTML identity where it
is needed to distinguish repeated paragraph numbers. It must not contain the
dated URL segment.

### Provision version

Keep the existing `node_type='rule'` for compatibility with current readers,
scripts and API consumers, but give it an explicit version identity:

```text
provision_version:<canonical-provision-key>:<rulebook-date>
```

Version metadata includes `identity_type='provision_version'`, the canonical
provision ID, the source page ID, the snapshot ID, the Rulebook date and the
original HTML ID. The version retains the source text and dated URL.

The parser emits one canonical node and one version node per parsed rule, and
adds a `has_version` edge from the canonical node to the version. Existing
structural `contains` edges continue to place version text below the dated Part
and Chapter, while `sourced_from` records the provenance relationship without
changing the reading spine.

### Source page and snapshot

The dated `part` node remains the source-page node for compatibility with the
Part rail. Its metadata identifies it as `identity_type='source_page'` and
records the date-free Part key and snapshot ID.

Add `document_snapshot`, keyed by URL and content hash, containing immutable
HTML, extracted text, fetch time and source-page linkage. The existing
`document_source` row remains the current logical page record and continues to
serve cached parsing, but no longer represents the complete snapshot history.

Add `node_alias` to preserve old node IDs and stable keys when the migration
changes a dated Rule identity to its version key.

## Identity and relationship rules

- The trailing `DD-MM-YYYY` path segment is source/version data, never part of
  a canonical provision key.
- The rule number alone is not assumed to be globally unique. The canonical
  key retains the existing chapter/HTML structural suffix where present.
- Semantic references to a Rule version are normalised to the canonical
  provision node. The original version ID is retained in edge or occurrence
  metadata when it is useful for date-specific provenance.
- Structural `contains` edges remain version/page-specific so the reader can
  display the text for the selected dated page.
- Analysis projections collapse version IDs to canonical provision IDs before
  calculating graph connectivity, centrality or communities.
- A reader opened on a dated source page continues to use that page's version
  nodes and does not show canonical identity nodes as prose.

## Migration and compatibility

Migration 8 is transactional and idempotent. It will:

1. create `document_snapshot` and `node_alias`;
2. derive canonical and version keys for existing Rule nodes;
3. create missing canonical nodes and identity edges;
4. remap version node IDs, graph endpoints, reference occurrences, embeddings
   and JSON metadata while recording old IDs/keys in `node_alias`;
5. normalise rule-targeting semantic edges to canonical targets; and
6. rebuild canonical and FTS projections.

The migration does not delete source HTML or reference evidence. It will run
against a backup copy in tests before being applied to the live local database.

## API and UI

Rulebook API node payloads expose the identity metadata already carried by the
node, including canonical provision ID, version date and source snapshot ID.
The normal Part rail and reader remain version-oriented. The graph can expose
`provision`, `has_version` and `sourced_from` when those types/relationships are
requested, but they are not added to the default reading representation.

## Error handling

- A URL without a trailing Rulebook date uses the parsed page date; if neither
  is available, the version key uses a deterministic `undated:<snapshot-id>`
  suffix rather than silently becoming canonical.
- Conflicting canonical keys fail the migration transaction with a diagnostic
  containing both source node IDs and keys.
- A missing target node does not block snapshot storage; it remains a normal
  unresolved reference until the existing repair workflow handles it.
- Re-running the same scrape with unchanged HTML is idempotent: it updates the
  current source row but creates no second version or snapshot.

## Testing

Add tests for:

- date stripping and canonical/version key generation;
- parser output for two dates producing one canonical node and two versions;
- immutable snapshot deduplication and same-URL content changes;
- migration remapping, aliases, edge endpoints and occurrence endpoints;
- canonical graph-analysis projection; and
- reader contents continuing to show the selected version text.

Run the existing Python suite and frontend suite, plus a live migration audit
against a copied database before applying the migration to the local serving
database.
