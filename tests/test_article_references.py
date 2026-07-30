from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.rulebook_scraper.article_references import (
    expand_citation_articles,
    explicit_instrument,
    extract_article_citations,
    is_non_reference_article_use,
    load_official_uk_crr_articles,
)
from scripts.backfill_uk_crr_article_references import (
    audit_and_apply,
    hydrate_internal_target,
)


OFFICIAL_XML = """\
<Legislation xmlns="http://www.legislation.gov.uk/namespaces/legislation">
  <P1group>
    <Title>Settlement/delivery risk</Title>
    <P1 DocumentURI="http://www.legislation.gov.uk/eur/2013/575/article/378">
      <P1para><Text>Institutions shall calculate the price difference.</Text></P1para>
    </P1>
  </P1group>
  <P1group>
    <Title>Free deliveries</Title>
    <P1 DocumentURI="http://www.legislation.gov.uk/eur/2013/575/article/379">
      <P1para><Text>An institution shall hold own funds for free deliveries.</Text></P1para>
    </P1>
  </P1group>
  <P1group>
    <Title>Waiver</Title>
    <P1 DocumentURI="http://www.legislation.gov.uk/eur/2013/575/article/380">
      <P1para><Text>A competent authority may waive Articles 378 and 379.</Text></P1para>
    </P1>
  </P1group>
</Legislation>
"""


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE node (
          id TEXT PRIMARY KEY,
          node_type TEXT NOT NULL,
          stable_key TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          text TEXT DEFAULT '',
          url TEXT DEFAULT '',
          metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE edge (
          id TEXT PRIMARY KEY,
          from_node_id TEXT NOT NULL,
          to_node_id TEXT NOT NULL,
          edge_type TEXT NOT NULL,
          source_method TEXT NOT NULL,
          confidence REAL NOT NULL,
          evidence_text TEXT DEFAULT '',
          source_url TEXT DEFAULT '',
          metadata_json TEXT DEFAULT '{}'
        );
        """
    )
    return conn


class ArticleReferenceTests(unittest.TestCase):
    def test_extracts_joined_ranges_and_multi_letter_articles(self) -> None:
        text = (
            "Articles 378, 379 and 380; Articles 399 to 403; "
            "Articles 428aa and 428ah."
        )
        citations = extract_article_citations(text)
        self.assertEqual(citations[0].bases, ("378", "379", "380"))
        self.assertEqual(citations[1].bases, ("399", "403"))
        self.assertEqual(citations[2].bases, ("428aa", "428ah"))

    def test_instrument_classifier_distinguishes_crr_internal_and_other(self) -> None:
        cases = [
            ("Article 380 of the CRR", "uk_crr"),
            ("CRR Articles 399 to 403", "uk_crr"),
            ("Article 429c(3) of this Chapter", "internal"),
            (
                "Article 53DA of the Regulated Activities Order in Part 3",
                "other",
            ),
            (
                "Art. 76(3)–(5) of the Solvency II Directive",
                "other",
            ),
            (
                "Article 16(5) first paragraph of MiFID II",
                "other",
            ),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                citation = extract_article_citations(text)[0]
                self.assertEqual(explicit_instrument(text, citation)[0], expected)

    def test_lexicalised_article_term_is_not_a_reference(self) -> None:
        text = "An Article 109 undertaking must notify the PRA."
        citation = extract_article_citations(text)[0]
        self.assertTrue(is_non_reference_article_use(text, citation))

    def test_authoritative_order_expands_ranges_and_loads_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "regulation.xml"
            path.write_text(OFFICIAL_XML, encoding="utf-8")
            order, articles = load_official_uk_crr_articles(path)
        citation = extract_article_citations("Articles 378 to 380")[0]
        expanded, errors = expand_citation_articles(citation, order)
        self.assertEqual(expanded, ["378", "379", "380"])
        self.assertEqual(errors, [])
        self.assertIn("competent authority may waive", articles["380"].text)
        self.assertEqual(
            articles["380"].url,
            "https://www.legislation.gov.uk/eur/2013/575/article/380",
        )

    def test_backfill_adds_joined_crr_edges_with_source_text(self) -> None:
        conn = make_db()
        conn.execute(
            """
            INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                "source",
                "rule",
                "source",
                "5.1",
                "In accordance with Article 380 of the CRR, Articles 378 and 379 "
                "of the CRR shall not apply.",
                "https://example.test/credit-risk#5.1",
                '{"part_title":"Credit Risk"}',
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            xml_path = Path(directory) / "regulation.xml"
            xml_path.write_text(OFFICIAL_XML, encoding="utf-8")
            override_path = Path(directory) / "overrides.json"
            with patch(
                "scripts.backfill_uk_crr_article_references.ensure_indexes"
            ):
                result = audit_and_apply(
                    conn,
                    source_xml=xml_path,
                    overrides_path=override_path,
                    apply=True,
                )
        self.assertEqual(result["summary"]["review_required"], 0)
        self.assertEqual(result["summary"]["edges_added"], 3)
        targets = {
            row["to_node_id"]
            for row in conn.execute(
                "SELECT to_node_id FROM edge WHERE from_node_id='source'"
            )
        }
        self.assertEqual(
            targets,
            {
                "external:uk-crr:article:378",
                "external:uk-crr:article:379",
                "external:uk-crr:article:380",
            },
        )
        target = conn.execute(
            "SELECT title,text FROM node WHERE id='external:uk-crr:article:380'"
        ).fetchone()
        self.assertIn("Waiver", target["title"])
        self.assertIn("competent authority may waive", target["text"])

    def test_empty_structural_target_is_hydrated_from_child_rules(self) -> None:
        conn = make_db()
        conn.executemany(
            "INSERT INTO node(id,node_type,stable_key,title,text) VALUES(?,?,?,?,?)",
            [
                ("article", "chapter", "article", "Article 380 Waiver", ""),
                ("p1", "rule", "p1", "Article 380(1)", "First paragraph."),
                ("p2", "rule", "p2", "Article 380(2)", "Second paragraph."),
            ],
        )
        conn.executemany(
            """
            INSERT INTO edge(
              id,from_node_id,to_node_id,edge_type,source_method,confidence
            ) VALUES(?,?,?,?,?,?)
            """,
            [
                ("a", "article", "p1", "contains", "site_structure", 1.0),
                ("b", "article", "p2", "contains", "site_structure", 1.0),
            ],
        )
        self.assertGreater(hydrate_internal_target(conn, "article"), 0)
        text = conn.execute(
            "SELECT text FROM node WHERE id='article'"
        ).fetchone()["text"]
        self.assertIn("First paragraph.", text)
        self.assertIn("Second paragraph.", text)


if __name__ == "__main__":
    unittest.main()
