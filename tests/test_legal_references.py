from __future__ import annotations

import unittest

from backend.rulebook_scraper.legal_references import (
    InstrumentRegistry,
    citation_occurrences,
    external_provision_node_id,
    fetch_official_provision,
)


AUDIT_COMMITTEE_24 = """
A firm must ensure that its audit committee performs at least the following
functions: (1) informs the governing body of the firm of the outcome of the
statutory audit; (2) monitors the financial reporting process; (3) monitors the
effectiveness of the firm’s internal quality control and risk management
systems; (4) monitors the statutory audit of the annual and consolidated
financial statements, taking into account any findings and conclusions of the
Financial Reporting Council Limited pursuant to Article 26(6) of the Statutory
Audit Regulation; (5) reviews and monitors the independence of the statutory
auditor or the audit firm in accordance with paragraphs 2(3), 2(4), 3, 4(1),
4(2), 5 to 8 and 10 to 12 of Schedule 1 to the Statutory Auditors and Third
Country Auditors Regulations 2016 (SI 2016/649) and Article 6 of the Statutory
Audit Regulation, and the suitability of non-audit services in accordance with
Article 5 of the Statutory Audit Regulation; and (6) is responsible for the
selection of the statutory auditor in accordance with Article 16 of the
Statutory Audit Regulation, except when Article 16(8) of the Statutory Audit
Regulation is applied. [Note: Art. 39(6) of the Statutory Audit Directive]
"""


class LegalReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = InstrumentRegistry.load()

    def test_preserves_dotted_article_paragraph_notation(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="mifid-definition",
            value=(
                "The term has the meaning given by article 4.1(17) of the "
                "Markets in Financial Instruments Directive 2004/39/EC."
            ),
            registry=self.registry,
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].group_text, "article 4.1(17)")
        self.assertEqual(occurrences[0].target.qualifiers, ("1", "17"))
        self.assertEqual(occurrences[0].provision_path, "article/4/1/17")
        self.assertEqual(occurrences[0].instrument.instrument_id, "mifid-i")

    def test_audit_committee_24_has_all_18_occurrences(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="audit-committee-2.4",
            value=" ".join(AUDIT_COMMITTEE_24.split()),
            registry=self.registry,
            source_title="Audit Committee 2.4",
        )

        self.assertEqual(len(occurrences), 18)
        self.assertEqual(
            sum(item.kind == "schedule_paragraph" for item in occurrences),
            12,
        )
        expected = {
            ("statutory-audit-regulation", "article/26/6"),
            ("statutory-audit-regulation", "article/6"),
            ("statutory-audit-regulation", "article/5"),
            ("statutory-audit-regulation", "article/16"),
            ("statutory-audit-regulation", "article/16/8"),
            ("statutory-audit-directive", "article/39/6"),
        }
        self.assertTrue(
            expected.issubset(
                {
                    (item.instrument.instrument_id, item.provision_path)
                    for item in occurrences
                    if item.instrument
                }
            )
        )
        schedule_group_ids = {
            item.group_id
            for item in occurrences
            if item.kind == "schedule_paragraph"
        }
        self.assertEqual(len(schedule_group_ids), 1)

    def test_qualified_article_targets_have_distinct_node_ids(self) -> None:
        instrument = self.registry.by_id["statutory-audit-regulation"]
        article = external_provision_node_id(instrument, "article/16")
        paragraph = external_provision_node_id(instrument, "article/16/8")
        self.assertNotEqual(article, paragraph)
        self.assertTrue(paragraph.endswith(":article:16:8"))

    def test_preceding_crr_does_not_drift_to_next_crd_citation(self) -> None:
        text = "(CRR Article 189, 20(6) and CRD Article 3(1)(7))"
        occurrences = citation_occurrences(
            source_node_id="policy-row",
            value=text,
            registry=self.registry,
            source_title="Policy table",
        )
        self.assertEqual(
            [
                (item.target.display, item.instrument.instrument_id)
                for item in occurrences
            ],
            [
                ("189", "uk-crr"),
                ("20(6)", "uk-crr"),
                ("3(1)(7)", "crd"),
            ],
        )

    def test_eu_suffix_identity_resolves_575_2013_before_amending_emir(self) -> None:
        text = (
            "Article 400(2)(c) of Regulation of the European Parliament and "
            "of the Council (575/2013/EU) on prudential requirements and "
            "amending Regulation (EU) No 648/2012"
        )
        occurrences = citation_occurrences(
            source_node_id="permission",
            value=text,
            registry=self.registry,
        )
        self.assertEqual(occurrences[0].instrument.instrument_id, "uk-crr")

    def test_cellar_loader_selects_the_annex_article_paragraph(self) -> None:
        payload = b"""
        <html><body>
          <p>Article 2</p><p>Main recommendation text.</p>
          <p>ANNEX</p>
          <p>Article 2</p><p>Staff headcount and financial ceilings</p>
          <p>1. The category of SMEs employs fewer than 250 persons.</p>
          <p>2. A small enterprise employs fewer than 50 persons.</p>
          <p>Article 3</p><p>Types of enterprise.</p>
        </body></html>
        """
        provision = fetch_official_provision(
            self.registry.by_id["commission-recommendation-sme"],
            "article/2/1",
            fetcher=lambda _: payload,
        )
        self.assertIn("fewer than 250 persons", provision.text)
        self.assertNotIn("fewer than 50 persons", provision.text)
        self.assertEqual(
            provision.url,
            "https://eur-lex.europa.eu/eli/reco/2003/361/oj",
        )

    def test_mapped_amending_source_preserves_the_cited_provision_title(self) -> None:
        payload = b"""
        <Legislation xmlns="http://www.legislation.gov.uk/namespaces/legislation"
                     xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>EEA Passport Rights Regulations 2018</dc:title>
          <P1group DocumentURI="http://www.legislation.gov.uk/uksi/2018/1149/regulation/23">
            <Title>Deemed approval for particular arrangements</Title>
            <P1><Pnumber>59ZZA</Pnumber><P1para><Text>Approval is deemed.</Text></P1para></P1>
          </P1group>
        </Legislation>
        """
        provision = fetch_official_provision(
            self.registry.by_id["fsma"],
            "section/59ZZA",
            fetcher=lambda _: payload,
        )
        self.assertTrue(
            provision.title.startswith(
                "Financial Services and Markets Act 2000 \u2014 Section 59ZZA"
            )
        )
        self.assertIn("Approval is deemed", provision.text)
        self.assertEqual(
            provision.url,
            "https://www.legislation.gov.uk/uksi/2018/1149/regulation/23",
        )


if __name__ == "__main__":
    unittest.main()
