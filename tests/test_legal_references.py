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

    def test_fsma_acronym_section_is_extracted(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-direct",
            value="A person appointed under section 59 of FSMA must comply.",
            registry=self.registry,
        )

        self.assertEqual(
            [(item.instrument.instrument_id, item.provision_path) for item in occurrences],
            [("fsma", "section/59")],
        )

    def test_fsma_acronym_without_of_is_extracted(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-without-of",
            value="The power in Section 425(1)(a) FSMA applies.",
            registry=self.registry,
        )

        self.assertEqual(
            [(item.instrument.instrument_id, item.provision_path) for item in occurrences],
            [("fsma", "section/425/1/a")],
        )

    def test_fsma_coordinated_sections_are_all_extracted(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-coordinated",
            value=(
                "The conditions in section 60(2A) or section 62A of FSMA and "
                "sections 166 and 166A FSMA apply."
            ),
            registry=self.registry,
        )

        self.assertEqual(
            [item.provision_path for item in occurrences],
            ["section/60/2A", "section/62A", "section/166", "section/166A"],
        )
        self.assertTrue(all(item.instrument.instrument_id == "fsma" for item in occurrences))

    def test_fsma_subsection_continuation_inherits_section_number(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-subsection-continuation",
            value="The requirements in section 183(1) and (2) of FSMA apply.",
            registry=self.registry,
        )

        self.assertEqual(
            [item.provision_path for item in occurrences],
            ["section/183/1", "section/183/2"],
        )

    def test_fsma_s_shorthand_and_bare_number_are_extracted(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-shorthand",
            value=(
                "The powers in s 312L(1) of FSMA and "
                "138EA of FSMA 2000 are relevant."
            ),
            registry=self.registry,
        )

        self.assertEqual(
            [item.provision_path for item in occurrences],
            ["section/312L/1", "section/138EA"],
        )

    def test_fsma_2023_is_not_resolved_to_fsma_2000(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-2023",
            value=(
                "The procedure is modified by section 19(4) of the "
                "Financial Services and Markets Act 2023 (FSMA 2023)."
            ),
            registry=self.registry,
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].instrument.instrument_id, "fsma-2023")
        self.assertEqual(occurrences[0].provision_path, "section/19/4")

    def test_bare_section_inherits_unambiguous_fsma_context(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-context",
            value=(
                "FSMA means the Financial Services and Markets Act 2000. "
                "The regulator may act under section 55M."
            ),
            registry=self.registry,
        )

        self.assertEqual(
            [(item.instrument.instrument_id, item.provision_path) for item in occurrences],
            [("fsma", "section/55M")],
        )

    def test_internal_section_label_is_not_recovered_as_fsma(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-internal-section",
            value=(
                "This chapter concerns FSMA. The process in section 3 of this "
                "Chapter applies."
            ),
            registry=self.registry,
        )

        self.assertEqual(occurrences, [])

    def test_ocr_footnote_after_fsma_does_not_hide_section(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-ocr-footnote",
            value="The power is set out in section 312J(2) of FSMA10 and applies.",
            registry=self.registry,
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].instrument.instrument_id, "fsma")
        self.assertEqual(occurrences[0].provision_path, "section/312J/2")

    def test_spaced_ocr_footnote_before_fsma_does_not_hide_section(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-spaced-footnote",
            value="The information power is in section 165 3 of FSMA.",
            registry=self.registry,
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].instrument.instrument_id, "fsma")
        self.assertEqual(occurrences[0].provision_path, "section/165")

    def test_table_layout_can_resolve_nearby_fsma_column(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-table",
            value=(
                "NOTICE DESCRIPTION ACT REFERENCE Warning Notice States the "
                "action which the PRA proposes Section 387 to take giving "
                "reasons for the proposed FSMA action."
            ),
            registry=self.registry,
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].instrument.instrument_id, "fsma")
        self.assertEqual(occurrences[0].provision_path, "section/387")

    def test_audited_hint_recovers_bare_fsma_section(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-hinted",
            value="The interviewee may be accompanied at a section 169(7) interview.",
            registry=self.registry,
            contextual_instrument_hints={"169": "fsma"},
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].instrument.instrument_id, "fsma")
        self.assertEqual(occurrences[0].provision_path, "section/169/7")

    def test_fsma_part_is_not_misread_as_section(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-part",
            value="The firm has permission under Part 4A of FSMA.",
            registry=self.registry,
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].kind, "part")
        self.assertEqual(occurrences[0].provision_path, "part/4A")

    def test_numeric_and_roman_fsma_parts_use_official_roman_path(self) -> None:
        numeric = citation_occurrences(
            source_node_id="fsma-part-23",
            value="The confidentiality provisions are in Part 23 of FSMA.",
            registry=self.registry,
        )
        roman = citation_occurrences(
            source_node_id="fsma-part-vii",
            value="The transfer is made under Part VII of FSMA.",
            registry=self.registry,
        )

        self.assertEqual(numeric[0].provision_path, "part/XXIII")
        self.assertEqual(roman[0].provision_path, "part/VII")

    def test_fsma_schedule_and_schedule_paragraph_paths_are_extracted(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-schedules",
            value=(
                "Paragraphs 31 and 35 of Schedule 1ZB of FSMA apply, while "
                "section 4F(3) of Schedule 6 to FSMA defines close links."
            ),
            registry=self.registry,
        )

        self.assertEqual(
            [(item.kind, item.provision_path) for item in occurrences],
            [
                ("schedule_paragraph", "schedule/1ZB/paragraph/31"),
                ("schedule_paragraph", "schedule/1ZB/paragraph/35"),
                ("schedule_paragraph", "schedule/6/paragraph/4F/3"),
            ],
        )

    def test_direct_fsma_schedule_is_extracted(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-schedule",
            value="The powers are listed in Schedule 17A to FSMA.",
            registry=self.registry,
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].kind, "schedule")
        self.assertEqual(occurrences[0].provision_path, "schedule/17A")

    def test_fsma_schedule_part_and_subparagraph_are_extracted(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-schedule-details",
            value=(
                "Part 3 (Penalties and Fees) of Schedule 1ZB to FSMA and "
                "sub-paragraph 3(2) of Schedule 19C to FSMA apply."
            ),
            registry=self.registry,
        )

        self.assertEqual(
            [item.provision_path for item in occurrences],
            [
                "schedule/1ZB/part/3",
                "schedule/19C/paragraph/3/2",
            ],
        )

    def test_fsma_part_chapter_is_extracted(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-part-chapter",
            value="The objectives are set out in Part 1A, chapter 2 of FSMA.",
            registry=self.registry,
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].kind, "part_chapter")
        self.assertEqual(occurrences[0].provision_path, "part/1A/chapter/2")
        self.assertEqual(occurrences[0].citation_text, "Part 1A, Chapter 2")

    def test_fused_fsma_footnote_is_removed_from_section_number(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-fused-footnote",
            value="A copy is given under section 39316 of FSMA.17",
            registry=self.registry,
        )

        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].provision_path, "section/393")

    def test_compact_s_shorthand_is_extracted(self) -> None:
        occurrences = citation_occurrences(
            source_node_id="fsma-compact-s",
            value="The firm acts under s138BA of FSMA and s.312R.",
            registry=self.registry,
        )

        self.assertEqual(
            [item.provision_path for item in occurrences],
            ["section/138BA", "section/312R"],
        )


if __name__ == "__main__":
    unittest.main()
