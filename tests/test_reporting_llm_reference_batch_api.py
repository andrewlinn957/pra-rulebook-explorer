import importlib.util
import sqlite3
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reporting_llm_reference_batch_api.py"
spec = importlib.util.spec_from_file_location("reporting_llm_reference_batch_api", SCRIPT_PATH)
reporting_refs = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(reporting_refs)


PRA110_TYPO_REFERENCE = {
    "reference_text": "the EBA’s instructions for completing the Maturity Ladder template of Annex XXII",
    "target_kind": "instruction",
    "target_title_or_identifier": "EBA’s instructions for completing the Maturity Ladder template of Annex XXII",
    "target_part_or_document": "Annex XXII",
    "evidence_quote": "The instructions build on the EBA’s instructions for completing the Maturity Ladder template of Annex XXII.",
}


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE graph_node (
          node_id TEXT PRIMARY KEY,
          node_type TEXT NOT NULL,
          label TEXT,
          source_table TEXT,
          source_pk TEXT,
          properties_json TEXT,
          review_status TEXT NOT NULL DEFAULT 'unreviewed'
        );
        """
    )
    return conn


class ReportingReferenceCorrectionTests(unittest.TestCase):
    def test_pra110_maturity_ladder_typo_maps_to_eba_annex_xxiii(self):
        correction = reporting_refs.curated_reference_correction(
            reporting_refs.PRA110_INSTRUCTIONS_SOURCE_ID,
            PRA110_TYPO_REFERENCE,
        )

        self.assertIsNotNone(correction)
        self.assertEqual(correction["corrected_from"], "Annex XXII")
        self.assertIn("Annex XXIII", correction["label"])
        self.assertIn("1025608", correction["url"])

    def test_same_words_from_an_unreviewed_source_are_not_silently_rewritten(self):
        correction = reporting_refs.curated_reference_correction(
            "another-source",
            PRA110_TYPO_REFERENCE,
        )

        self.assertIsNone(correction)

    def test_resolver_prefers_curated_target_over_literal_annex_xxii(self):
        conn = make_conn()
        conn.execute(
            "INSERT INTO graph_node VALUES (?,?,?,?,?,?,?)",
            (
                "source_document:wrong-annex",
                "SourceDocument",
                "Annex XXII (PDF)",
                "source_document",
                "wrong-annex",
                "{}",
                "accepted_candidate",
            ),
        )
        reporting_refs.ensure_curated_reference_targets(conn)

        target, method, score = reporting_refs.Resolver(conn).resolve(
            PRA110_TYPO_REFERENCE,
            reporting_refs.PRA110_INSTRUCTIONS_SOURCE_ID,
        )

        self.assertEqual(method, "curated_source_correction")
        self.assertEqual(score, 1.0)
        self.assertEqual(target["node_type"], "ExternalReference")
        self.assertIn("Annex XXIII", target["label"])
        self.assertIn("1025608", target["props"]["url"])

    def test_resolver_rejects_a_source_document_targeting_itself(self):
        source = {
            "node_id": "source_document:annex-ii",
            "node_type": "SourceDocument",
            "label": "Annex II (PDF)",
        }

        target, method, score = reporting_refs.reject_self_reference(
            source["node_id"],
            source,
            "annex_source_document",
            0.86,
        )

        self.assertIsNone(target)
        self.assertEqual(method, "self_reference_rejected")
        self.assertEqual(score, 0.0)


if __name__ == "__main__":
    unittest.main()
