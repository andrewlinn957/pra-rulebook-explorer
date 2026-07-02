import json
import unittest

from scripts.extract_monetary_thresholds_for_inflation import (
    classify_candidate_without_llm,
    is_monetary_candidate,
    parse_llm_decision,
)


class MonetaryThresholdClassificationTests(unittest.TestCase):
    def test_is_monetary_candidate_requires_currency_amount(self):
        self.assertTrue(is_monetary_candidate("A firm must hold £1 million in capital."))
        self.assertTrue(is_monetary_candidate("The threshold is EUR 5 million."))
        self.assertFalse(is_monetary_candidate("The firm must report within 30 days."))
        self.assertFalse(is_monetary_candidate("The rule refers to Article 92 CRR."))

    def test_rule_based_classifier_rejects_pure_accounting_or_worked_example_mentions(self):
        self.assertEqual(
            classify_candidate_without_llm("The carrying value of the asset was £100,000 at year end.")[0],
            "exclude",
        )
        self.assertEqual(
            classify_candidate_without_llm(
                "For example, if a client has a transactional account of £100,000, the amount reported is..."
            )[0],
            "exclude",
        )

    def test_rule_based_classifier_keeps_obvious_thresholds_for_llm_review(self):
        decision, reason = classify_candidate_without_llm(
            "Only firms with total assets exceeding £15 billion are required to submit this return."
        )
        self.assertEqual(decision, "review")
        self.assertTrue("threshold" in reason.lower() or "trigger" in reason.lower())

    def test_parse_llm_decision_normalises_schema_and_defaults(self):
        raw = json.dumps({
            "is_threshold": True,
            "inflation_index_candidate": True,
            "threshold_amounts": ["£15 billion"],
            "threshold_type": "scope_trigger",
            "rationale": "Eligibility threshold for reporting.",
        })
        parsed = parse_llm_decision(raw)
        self.assertTrue(parsed["is_threshold"])
        self.assertTrue(parsed["inflation_index_candidate"])
        self.assertEqual(parsed["threshold_amounts"], ["£15 billion"])
        self.assertEqual(parsed["threshold_type"], "scope_trigger")

    def test_parse_llm_decision_handles_invalid_json_conservatively(self):
        parsed = parse_llm_decision("not json")
        self.assertFalse(parsed["is_threshold"])
        self.assertFalse(parsed["inflation_index_candidate"])
        self.assertIn("parse", parsed["rationale"].lower())


if __name__ == "__main__":
    unittest.main()
