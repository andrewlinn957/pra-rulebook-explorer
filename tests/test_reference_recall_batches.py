import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reference_recall_batches.py"
spec = importlib.util.spec_from_file_location("reference_recall_batches", SCRIPT_PATH)
reference_recall_batches = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(reference_recall_batches)


def test_make_request_preserves_absolute_chunk_offsets_and_schema():
    row = {
        "source_node_id": "source-1",
        "source_node_type": "rule",
        "source_title": "2.4",
        "source_url": "https://example.test/rule#x",
        "source_text_hash": "hash-1",
        "chunk_start": 6000,
        "chunk_end": 6120,
        "text": "See Article 26(6) of the Statutory Audit Regulation.",
        "candidates": [
            {
                "span_start": 6004,
                "span_end": 6060,
                "candidate_text": "Article 26(6) of the Statutory Audit Regulation",
                "candidate_kind": "legal_citation",
                "status": "tail_unreviewed",
            }
        ],
    }
    request = reference_recall_batches.make_request(row, "test-model")
    assert request["custom_id"]
    assert request["body"]["model"] == "test-model"
    prompt = request["body"]["messages"][1]["content"]
    assert "chunk_start: 6000" in prompt
    assert '"span_start": 0' in prompt
    assert "Article 26(6) of the Statutory Audit Regulation" in prompt


def test_prepare_writes_independent_jsonl_batches_and_manifest(tmp_path):
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
                "chunk_end": 10,
                "text": "Article 26.",
                "candidates": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "batches"
    manifest = reference_recall_batches.prepare(pilot, output, "test-model", None, 1)
    assert manifest["request_count"] == 1
    assert len(manifest["batch_paths"]) == 1
    line = (output / "batch-0001.jsonl").read_text(encoding="utf-8").strip()
    request = json.loads(line)
    assert request["method"] == "POST"
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8"))["status"] == "prepared_read_only"
