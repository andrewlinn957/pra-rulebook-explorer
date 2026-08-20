# Ingestion output reconciliation design

## Scope

Make every Rulebook ingestion scope reconcile its current output against the
previous successful output. A successful refresh is authoritative for that
source scope: page/version nodes and edges that are no longer emitted are
removed from the live graph when no other ingestion scope still owns them.
Immutable `document_snapshot` rows are never removed.

This change covers the scraper's page/index outputs and the deterministic
derived-edge pass invoked by `scrape --derive`. It does not make an unrelated
reference-resolution or embedding job part of a page refresh.

## Current problem

The scraper stores each parsed node and edge with an upsert. It has no record
of which objects a particular successful fetch produced, so an object omitted
by a later fetch is indistinguishable from an object that should remain. The
same problem affects source-owned derived edges. A failed fetch or parser
exception must not be interpreted as an empty successful output.

## Ownership model

Add three small control tables:

```text
ingestion_run
  one row per command invocation, with running/completed/failed status

ingestion_run_scope
  one row per target scope in a run, with success/failure and counts

ingestion_output
  the latest successful node/edge membership for each scope
```

A source scope key is deterministic from the normalised source URL and source
type. A derived scope uses a deterministic synthetic key, for example
`derived:richer-edges`. `ingestion_output` stores the object type, live object
ID, payload hash and the successful run ID.

The same node or edge may be owned by multiple scopes. Reconciliation removes
the scope's old membership first, then deletes the live object only when no
other current scope owns it. This is important for canonical provision nodes,
which may be emitted by more than one dated page, and for index nodes that are
also emitted by their detail page.

## Successful source refresh

The command fetches and parses a target completely before opening the
reconciliation transaction. The transaction then:

1. stores the new immutable snapshot and updates the current
   `document_source` row;
2. upserts the parsed nodes and edges, replacing source-managed node metadata
   rather than JSON-merging fields that may have disappeared;
3. compares the previous scope membership with the newly emitted membership;
4. removes stale edge memberships and deletes stale edges with no remaining
   owner, deleting their occurrences first;
5. removes stale node memberships and deletes page/version nodes with no
   remaining owner, cleaning incident edges, occurrences, aliases, embeddings,
   search rows and canonical projection rows as applicable; and
6. records the new membership and marks the scope successful.

Edges are removed before nodes so no live edge is left with a missing endpoint.
If an edge from another scope points to a node that is being deleted, that
dependent edge is removed as well and its manifest membership is discarded.
Shared canonical targets are retained when they still have a version or an
independent owner.

When a live node's payload hash changes, source-linked materialised reference
occurrences are invalidated for that source node. This prevents old text spans
from surviving a changed provision body; the existing occurrence materialiser
can recreate them from the new text.

## Failure and partial-run rules

Fetch errors, parse errors and failed completeness checks mark only that scope
failed. They do not replace its previous membership and do not delete live
objects. The run may finish as `completed` when every scope succeeded or
`partial` when at least one scope failed.

`--part`, `--guidance` and other targeted invocations reconcile only the
requested scopes. They never prune scopes outside the invocation. A full
`--all-parts` run can therefore remove a part that a successfully fetched
Rulebook index no longer lists, while a targeted run cannot accidentally prune
the rest of the corpus.

## Legacy database bootstrap

Existing databases have no output manifest. On the first successful refresh of
a scope, the reconciler bootstraps only the likely legacy objects for that
scope from source URL, source-page metadata and source-owned edge endpoints.
It then applies the same comparison. Orphaned placeholder nodes are removed
only when they have no remaining edge, so unresolved targets from other pages
are preserved.

## Derived outputs

`derive_richer_edges` is reconciled under its own deterministic derived scope.
Its known source methods are treated as one replaceable output set, allowing a
new derive pass to remove an edge that no longer satisfies the current corpus
without touching explicit parser edges, legal-reference edges or embedding
edges. The derived scope is updated only after the derivation completes.

## Snapshot and rollback guarantees

Every fetched URL/content pair remains in `document_snapshot`, including the
HTML that caused a node or edge to be removed. The current graph is therefore
reversible by replaying an earlier snapshot through the parser. The
reconciliation transaction is atomic for each scope, and the run ledger makes
partial or failed refreshes visible to audits.

## Verification

Tests will cover stale node/edge removal, immutable snapshot retention,
idempotent refreshes, shared ownership, metadata replacement, occurrence and
projection cleanup, failed/partial scope preservation, legacy bootstrap and
derived-edge replacement. The live verification will run the full backend and
frontend suites, an ingestion reconciliation audit and a controlled refresh
against a copied database before the serving database is updated.
