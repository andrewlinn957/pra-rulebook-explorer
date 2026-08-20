import unittest

from backend.rulebook_scraper.legal_identity import (
    canonical_part_key,
    canonical_provision_key,
    normalise_rulebook_date,
    provision_version_key,
    snapshot_id,
    source_page_key,
)


PART_JUNE = "https://www.prarulebook.co.uk/pra-rules/liquidity-coverage-ratio-crr/01-06-2026"
PART_JULY = "https://www.prarulebook.co.uk/pra-rules/liquidity-coverage-ratio-crr/01-07-2026"


class LegalIdentityTests(unittest.TestCase):
    def test_date_free_part_identity_is_shared_by_dated_pages(self):
        self.assertEqual(canonical_part_key(PART_JUNE), canonical_part_key(PART_JULY))
        self.assertEqual(
            canonical_part_key(PART_JUNE),
            "part:pra-rules/liquidity-coverage-ratio-crr",
        )

    def test_normalise_rulebook_date_accepts_common_source_formats(self):
        self.assertEqual(normalise_rulebook_date("01-06-2026"), "01-06-2026")
        self.assertEqual(normalise_rulebook_date("01/06/2026"), "01-06-2026")
        self.assertEqual(normalise_rulebook_date("2026-06-01"), "01-06-2026")
        self.assertIsNone(normalise_rulebook_date("not-a-date"))

    def test_canonical_provision_key_keeps_structural_context(self):
        key = canonical_provision_key(
            PART_JUNE,
            structural_locator="chapter:2:article-10",
            rule_number="1",
        )
        self.assertEqual(
            key,
            "provision:pra-rules/liquidity-coverage-ratio-crr:chapter:2:article-10:1",
        )
        self.assertNotEqual(
            key,
            canonical_provision_key(PART_JUNE, "chapter:3:article-10", "1"),
        )

    def test_versions_differ_by_date_but_share_canonical_locator(self):
        canonical = canonical_provision_key(PART_JUNE, "chapter:2:article-10", "1")
        june = provision_version_key(canonical, "01-06-2026")
        july = provision_version_key(canonical, "01-07-2026")
        self.assertNotEqual(june, july)
        self.assertIn(":01-06-2026", june)
        self.assertIn(":01-07-2026", july)

    def test_undated_version_uses_snapshot_identity(self):
        canonical = canonical_provision_key(PART_JUNE, "chapter:2:article-10", "1")
        sid = snapshot_id(PART_JUNE, "<html>version</html>")
        self.assertEqual(
            provision_version_key(canonical, None, snapshot=sid),
            f"{canonical.replace('provision:', 'provision_version:', 1)}:undated:{sid}",
        )

    def test_source_page_keeps_the_date_and_snapshot_changes_with_content(self):
        self.assertNotEqual(source_page_key(PART_JUNE), source_page_key(PART_JULY))
        self.assertEqual(source_page_key(PART_JUNE), "source_page:pra-rules/liquidity-coverage-ratio-crr/01-06-2026")
        first = snapshot_id(PART_JUNE, "same")
        self.assertEqual(first, snapshot_id(PART_JUNE, "same"))
        self.assertNotEqual(first, snapshot_id(PART_JUNE, "changed"))
        self.assertNotEqual(first, snapshot_id(PART_JULY, "same"))


if __name__ == "__main__":
    unittest.main()
