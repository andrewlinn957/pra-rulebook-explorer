from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
import unittest

from backend.rulebook_scraper.legal_references import InstrumentRegistry
from backend.rulebook_scraper.reference_occurrences import (
    policy_citation_occurrences,
)
from scripts.apply_resolution_policy import Outcome, apply_outcomes


class ReferenceOccurrenceMaterializerTests(unittest.TestCase):
    def test_policy_row_expands_to_each_matching_textual_span(self) -> None:
        text = (
            "Article 114(2) of CRR ; (i) first. "
            "Article 114(2) of CRR ; (ii) second. "
            "Article 114(2) of CRR ; (iii) third."
        )
        row = {
            "reference_text": "Article 114(2) of CRR",
            "target_kind": "article",
            "target_title_or_identifier": "Article 114(2)",
            "target_part_or_document": "CRR",
            "evidence_quote": "Article 114(2) of CRR",
        }

        occurrences = policy_citation_occurrences(
            source_node_id="lcr-article-10-1",
            source_text=text,
            source_title="Article 10(1)",
            row=row,
            registry=InstrumentRegistry.load(),
        )

        self.assertEqual(
            [(item.span_start, item.span_end, item.instrument.instrument_id) for item in occurrences],
            [(8, 14, "uk-crr"), (43, 49, "uk-crr"), (80, 86, "uk-crr")],
        )

    def test_policy_apply_replaces_stale_target_and_materialises_each_span(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE edge (
              id TEXT PRIMARY KEY,
              from_node_id TEXT,
              to_node_id TEXT,
              edge_type TEXT,
              source_method TEXT,
              confidence REAL,
              evidence_text TEXT,
              source_url TEXT,
              metadata_json TEXT
            );
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT NOT NULL,
              stable_key TEXT NOT NULL,
              title TEXT NOT NULL,
              text TEXT DEFAULT '',
              url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE reference_occurrence (
              occurrence_id TEXT PRIMARY KEY,
              group_id TEXT,
              source_node_id TEXT,
              target_node_id TEXT,
              edge_id TEXT,
              relationship_type TEXT,
              citation_kind TEXT,
              citation_text TEXT,
              group_text TEXT,
              instrument_id TEXT,
              provision_path TEXT,
              qualifier TEXT,
              span_start INTEGER,
              span_end INTEGER,
              status TEXT,
              source_method TEXT,
              confidence REAL,
              context_text TEXT,
              metadata_json TEXT,
              created_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE llm_reference_resolution (
              id TEXT PRIMARY KEY,
              source_node_id TEXT,
              ref_index INTEGER,
              reference_text TEXT,
              target_kind TEXT,
              target_title_or_identifier TEXT,
              target_part_or_document TEXT,
              evidence_quote TEXT,
              extracted_confidence REAL,
              target_node_id TEXT,
              target_node_type TEXT,
              target_title TEXT,
              resolver_method TEXT,
              resolver_confidence REAL,
              already_had_edge INTEGER,
              added_edge_id TEXT,
              metadata_json TEXT,
              resolution_status TEXT,
              resolution_scope TEXT
            );
            """
        )
        text = (
            "Article 114(2) of CRR ; (i) first. "
            "Article 114(2) of CRR ; (ii) second. "
            "Article 114(2) of CRR ; (iii) third."
        )
        source = {
            "id": "source",
            "node_type": "rule",
            "title": "Article 10(1)",
            "text": text,
            "url": "",
            "meta": {},
        }
        target = {
            "id": "external:legislation:uk-crr:article:114",
            "node_type": "external_reference",
            "title": "UK CRR Article 114",
            "text": "Article 114 source text",
            "url": "https://www.legislation.gov.uk/eur/2013/575/article/114",
            "meta": {},
        }
        resolver = SimpleNamespace(
            nodes={source["id"]: source, target["id"]: target},
            registry=InstrumentRegistry.load(),
            conn=conn,
        )
        conn.execute(
            "INSERT INTO llm_reference_resolution VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "resolution",
                "source",
                0,
                "Article 114(2) of CRR",
                "article",
                "Article 114(2)",
                "CRR",
                "Article 114(2) of CRR",
                0.97,
                "internal",
                "part",
                "Liquidity Coverage Ratio (CRR)",
                "policy_internal_provision",
                0.97,
                1,
                "",
                json.dumps({"policy_scope": "provision"}),
                "resolved",
                "provision",
            ),
        )
        conn.execute(
            "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "old-edge",
                "source",
                "internal",
                "references",
                "resolution_policy_v1",
                0.97,
                "old",
                "",
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO reference_occurrence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "old-occurrence",
                "old-group",
                "source",
                "internal",
                "old-edge",
                "REF",
                "article",
                "Article 114(2) of CRR",
                "Article 114(2) of CRR",
                None,
                None,
                "",
                0,
                24,
                "materialized",
                "resolution_policy_v1",
                0.97,
                text,
                json.dumps({"resolution_id": "resolution"}),
                "",
                "",
            ),
        )
        conn.commit()

        outcome = Outcome(
            resolution_id="resolution",
            source_id="source",
            status="resolved",
            scope="provision",
            target_id=target["id"],
            target_type=target["node_type"],
            target_title=target["title"],
            target_url=target["url"],
            target_text_available=True,
            resolver_method="policy_external_parent_provision_existing",
            confidence=0.97,
            span_start=0,
            span_end=24,
            quoted_text="Article 114(2) of CRR",
            instrument_id="uk-crr",
            provision_path="article/114/2",
        )
        counts = apply_outcomes(conn, resolver, [outcome], {}, {})

        self.assertEqual(counts["materialized_occurrences"], 3)
        self.assertIsNone(conn.execute("SELECT 1 FROM edge WHERE id='old-edge'").fetchone())
        rows = conn.execute(
            "SELECT target_node_id,instrument_id,span_start,span_end FROM reference_occurrence ORDER BY span_start"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (target["id"], "uk-crr", 8, 14),
                (target["id"], "uk-crr", 43, 49),
                (target["id"], "uk-crr", 80, 86),
            ],
        )

        # Reprocessing the same ledger row exercises the SQL UPDATE path and
        # must keep the same three lexical identities rather than adding
        # duplicates.
        second_counts = apply_outcomes(conn, resolver, [outcome], {}, {})
        self.assertEqual(second_counts["materialized_occurrences"], 3)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM reference_occurrence").fetchone()[0],
            3,
        )

    def test_policy_materialisation_canonicalises_version_targets(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE edge (
              id TEXT PRIMARY KEY, from_node_id TEXT, to_node_id TEXT,
              edge_type TEXT, source_method TEXT, confidence REAL,
              evidence_text TEXT, source_url TEXT, metadata_json TEXT
            );
            CREATE TABLE node (
              id TEXT PRIMARY KEY, node_type TEXT NOT NULL,
              stable_key TEXT NOT NULL, title TEXT NOT NULL,
              text TEXT DEFAULT '', url TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE reference_occurrence (
              occurrence_id TEXT PRIMARY KEY, group_id TEXT, source_node_id TEXT,
              target_node_id TEXT, edge_id TEXT, relationship_type TEXT,
              citation_kind TEXT, citation_text TEXT, group_text TEXT,
              instrument_id TEXT, provision_path TEXT, qualifier TEXT,
              span_start INTEGER, span_end INTEGER, status TEXT,
              source_method TEXT, confidence REAL, context_text TEXT,
              metadata_json TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE llm_reference_resolution (
              id TEXT PRIMARY KEY, source_node_id TEXT, ref_index INTEGER,
              reference_text TEXT, target_kind TEXT, target_title_or_identifier TEXT,
              target_part_or_document TEXT, evidence_quote TEXT,
              extracted_confidence REAL, target_node_id TEXT, target_node_type TEXT,
              target_title TEXT, resolver_method TEXT, resolver_confidence REAL,
              already_had_edge INTEGER, added_edge_id TEXT, metadata_json TEXT,
              resolution_status TEXT, resolution_scope TEXT
            );
            """
        )
        source = {
            "id": "source",
            "node_type": "rule",
            "title": "Article 10(1)",
            "text": "See Article 2(1).",
            "url": "",
            "meta": {},
        }
        canonical = {
            "id": "canonical",
            "node_type": "provision",
            "stable_key": "provision:pra-rules/test:chapter:2:article-2:1",
            "title": "Article 2(1)",
            "text": "",
            "url": "",
            "meta": {"identity_type": "canonical_provision"},
        }
        version = {
            "id": "version",
            "node_type": "rule",
            "stable_key": "provision_version:pra-rules/test:chapter:2:article-2:1:01-06-2026",
            "title": "Article 2(1)",
            "text": "Dated Article 2(1) text",
            "url": "https://www.prarulebook.co.uk/pra-rules/test/01-06-2026#article-2-1",
            "meta": {
                "identity_type": "provision_version",
                "canonical_provision_id": "canonical",
                "rulebook_date": "01-06-2026",
            },
        }
        for node in (canonical, version):
            conn.execute(
                "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
                (node["id"], node["node_type"], node["stable_key"], node["title"], node["text"], node["url"], json.dumps(node["meta"])),
            )
        conn.execute(
            "INSERT INTO llm_reference_resolution VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("resolution", "source", 0, "Article 2(1)", "article", "Article 2(1)", "Test", "Article 2(1)", 0.97, "version", "rule", "Article 2(1)", "test", 0.97, 0, "", "{}", "resolved", "provision"),
        )
        resolver = SimpleNamespace(
            conn=conn,
            nodes={source["id"]: source, canonical["id"]: canonical, version["id"]: version},
            registry=InstrumentRegistry.load(),
        )
        outcome = Outcome(
            resolution_id="resolution",
            source_id="source",
            status="resolved",
            scope="provision",
            target_id="version",
            target_type="rule",
            target_title="Article 2(1)",
            target_url=version["url"],
            target_text_available=True,
            resolver_method="test",
            confidence=0.97,
            span_start=4,
            span_end=15,
            quoted_text="Article 2(1)",
        )

        apply_outcomes(conn, resolver, [outcome], {}, {})

        assert conn.execute("SELECT to_node_id FROM edge").fetchone()[0] == "canonical"
        assert conn.execute("SELECT target_node_id FROM reference_occurrence").fetchone()[0] == "canonical"
        assert tuple(conn.execute("SELECT target_node_id,target_node_type FROM llm_reference_resolution").fetchone()) == ("canonical", "provision")


if __name__ == "__main__":
    unittest.main()
