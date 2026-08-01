import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from scripts.apply_resolution_policy import InstrumentRegistry, PolicyResolver, identifier_candidates


class ResolutionPolicyHelpersTest(unittest.TestCase):
    def test_identifier_candidates_preserve_dotted_and_alphanumeric_numbers(self):
        self.assertEqual(identifier_candidates("rule 3D21"), ["3D21"])
        self.assertEqual(identifier_candidates("rule 2C.9.1"), ["2C.9.1"])
        self.assertEqual(identifier_candidates("sections 2H and 3B"), ["2H", "3B"])

    def test_identifier_candidates_capture_qualified_article(self):
        self.assertEqual(
            identifier_candidates("Articles 433(b) and 433(c)"),
            ["433(b)", "433", "433(c)"],
        )

    def test_fca_sup_tp_split_pdf_heading_is_materialised_as_provision(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE node (id TEXT, node_type TEXT, stable_key TEXT, title TEXT, text TEXT, url TEXT, metadata_json TEXT)"
        )
        resolver = PolicyResolver(
            conn,
            InstrumentRegistry.load(Path(__file__).resolve().parents[1] / "config" / "legal_instruments.json"),
        )
        row = {
            "reference_text": "SUP TP 11.2.11R",
            "target_title_or_identifier": "SUP TP 11.2.11R",
            "target_part_or_document": "FCA Handbook",
            "evidence_quote": "SUP TP 11.2.11R",
        }
        split_pdf_text = (
            "SUP TP      R          (1)  This is the cited transitional rule.\n"
            "11.2.11                             The authoritative wording.\n"
            "SUP TP      G          A later rule.\n"
            "11.2.12                             Later wording.\n"
        )
        with patch("scripts.apply_resolution_policy.special_document_text", return_value=split_pdf_text):
            target = resolver.create_fca_handbook_provision_target(row, {})
        self.assertIsNotNone(target)
        self.assertEqual(target["title"], "FCA SUP TP — 11.2.11R")
        self.assertIn("authoritative wording", target["text"])
        self.assertEqual(target["meta"]["instrument_id"], "fca-sup-tp")

    def test_fca_alphanumeric_sup_section_is_materialised_as_provision(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE node (id TEXT, node_type TEXT, stable_key TEXT, title TEXT, text TEXT, url TEXT, metadata_json TEXT)"
        )
        resolver = PolicyResolver(
            conn,
            InstrumentRegistry.load(Path(__file__).resolve().parents[1] / "config" / "legal_instruments.json"),
        )
        row = {
            "reference_text": "SUP 10C.4.3R",
            "target_title_or_identifier": "SUP 10C.4.3R",
            "target_part_or_document": "FCA Handbook",
            "evidence_quote": "SUP 10C.4.3R",
        }
        pdf_text = (
            "SUP 10C.4.3R       The cited rule wording.\n"
            "SUP 10C.4.4       The following rule.\n"
        )
        with patch("scripts.apply_resolution_policy.special_document_text", return_value=pdf_text):
            target = resolver.create_fca_handbook_provision_target(row, {})
        self.assertIsNotNone(target)
        self.assertEqual(target["meta"]["provision_paths"], ["sup/10C/4/3"])
        self.assertIn("cited rule wording", target["text"])


if __name__ == "__main__":
    unittest.main()
