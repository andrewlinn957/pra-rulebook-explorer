import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_reference_recall_reviews.py"
spec = importlib.util.spec_from_file_location("validate_reference_recall_reviews", SCRIPT_PATH)
validate_reference_recall_reviews = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validate_reference_recall_reviews)


def test_validate_accepts_exact_absolute_span(tmp_path):
    pilot = tmp_path / "pilot.jsonl"
    row = {
        "source_node_id": "source-1",
        "source_node_type": "rule",
        "source_title": "2.4",
        "source_url": "",
        "source_text_hash": "hash-1",
        "chunk_start": 6000,
        "chunk_end": 6060,
        "text": "See Article 26(6) of the Statutory Audit Regulation.",
        "candidates": [],
    }
    pilot.write_text(json.dumps(row) + "\n", encoding="utf-8")
    custom_id = validate_reference_recall_reviews.request_id("source-1", "hash-1", 6000, 6060)
    quote = "Article 26(6) of the Statutory Audit Regulation"
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps(
            {
                "custom_id": custom_id,
                "source_node_id": "source-1",
                "source_text_hash": "hash-1",
                "findings": [
                    {
                        "span_start": 6004,
                        "span_end": 6004 + len(quote),
                        "quoted_text": quote,
                        "target_hint": "Article 26(6) Statutory Audit Regulation",
                        "target_kind": "article",
                        "decision": "REFERENCE",
                        "confidence": 0.98,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = validate_reference_recall_reviews.validate(pilot, responses)
    assert report["valid"] is True
    assert report["valid_findings"] == 1


def test_validate_rejects_non_exact_quote_and_span(tmp_path):
    pilot = tmp_path / "pilot.jsonl"
    pilot.write_text(
        json.dumps(
            {
                "source_node_id": "source-1",
                "source_node_type": "rule",
                "source_title": "2.4",
                "source_url": "",
                "source_text_hash": "hash-1",
                "chunk_start": 0,
                "chunk_end": 20,
                "text": "See Article 26.",
                "candidates": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    custom_id = validate_reference_recall_reviews.request_id("source-1", "hash-1", 0, 20)
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps(
            {
                "custom_id": custom_id,
                "findings": [
                    {
                        "span_start": 0,
                        "span_end": 4,
                        "quoted_text": "Article 27",
                        "target_kind": "article",
                        "decision": "REFERENCE",
                        "confidence": 0.5,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = validate_reference_recall_reviews.validate(pilot, responses)
    assert report["valid"] is False
    assert report["valid_findings"] == 0
    assert report["invalid_records"][0]["errors"] == ["quoted_text_not_exact_substring"]


def test_validate_can_accept_a_valid_partial_batch(tmp_path):
    pilot = tmp_path / "pilot.jsonl"
    rows = []
    for source_id in ("source-1", "source-2"):
        rows.append(
            {
                "source_node_id": source_id,
                "source_node_type": "rule",
                "source_title": source_id,
                "source_url": "",
                "source_text_hash": f"hash-{source_id}",
                "chunk_start": 0,
                "chunk_end": 20,
                "text": "See Article 26.",
                "candidates": [],
            }
        )
    pilot.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    custom_id = validate_reference_recall_reviews.request_id("source-1", "hash-source-1", 0, 20)
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps(
            {
                "custom_id": custom_id,
                "findings": [
                    {
                        "span_start": 4,
                        "span_end": 14,
                        "quoted_text": "Article 26",
                        "target_kind": "article",
                        "decision": "REFERENCE",
                        "confidence": 0.9,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    strict = validate_reference_recall_reviews.validate(pilot, responses)
    partial = validate_reference_recall_reviews.validate(pilot, responses, allow_partial=True)
    assert strict["valid"] is False
    assert partial["valid"] is True
    assert partial["missing_custom_ids"]


def test_validate_rejects_malformed_target_kind_and_confidence(tmp_path):
    row = {
        "source_node_id": "source-1",
        "source_node_type": "rule",
        "source_title": "2.4",
        "source_url": "",
        "source_text_hash": "hash-1",
        "chunk_start": 0,
        "chunk_end": 20,
        "text": "See Article 26.",
        "candidates": [],
    }
    pilot = tmp_path / "pilot.jsonl"
    pilot.write_text(json.dumps(row) + "\n", encoding="utf-8")
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps(
            {
                "custom_id": validate_reference_recall_reviews.request_id("source-1", "hash-1", 0, 20),
                "findings": [
                    {
                        "span_start": 4,
                        "span_end": 14,
                        "quoted_text": "Article 26",
                        "target_hint": "Article 26",
                        "target_kind": "rule",
                        "confidence": "article",
                        "decision": "REFERENCE",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = validate_reference_recall_reviews.validate(pilot, responses)
    assert report["valid"] is False
    assert report["invalid_records"][0]["errors"] == ["invalid_confidence"]
