import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "llm_structural_context_pass.py"
spec = importlib.util.spec_from_file_location("llm_structural_context_pass", SCRIPT_PATH)
pass_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(pass_module)


def row(**values):
    defaults = {
        "id": "source",
        "node_type": "chapter",
        "stable_key": "source",
        "title": "Source chapter",
        "text": "",
        "url": "",
        "metadata_json": "{}",
    }
    defaults.update(values)
    return sqlite3.Row(sqlite3.connect(":memory:"), []) if False else defaults


def test_full_source_pack_keeps_late_candidate_and_metadata():
    source = row(
        text="Heading\nFirst provision.\n" + "x" * 100 + "\nRule 9.\nSee paragraph 2.4 of this Part.\n",
        metadata_json=json.dumps({"part_title": "Test Part", "chapter_number": "2"}),
    )
    candidate = {"candidate_id": "c1", "candidate_text": "paragraph 2.4", "candidate_kind": "structure_reference", "span_start": source["text"].index("paragraph 2.4"), "span_end": source["text"].index("paragraph 2.4") + len("paragraph 2.4"), "quoted_text": "paragraph 2.4"}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE edge (from_node_id TEXT,to_node_id TEXT,edge_type TEXT);")
    nodes = {source["id"]: source}
    indexes = {"exact_title": {}, "structural": {}}
    pack = pass_module.make_context_pack(source, [candidate], nodes, indexes, conn, max_tokens=1000, neighbour_blocks=2, pass_number=1)
    assert pack["full_source_included"] is True
    assert pack["source_metadata"]["part_title"] == "Test Part"
    assert "paragraph 2.4" in "\n".join(segment["text"] for segment in pack["segments"])
    assert pack["provenance"]["selected_ranges"] == [[0, len(source["text"])]]


def test_large_pack_is_span_centred_and_under_token_cap():
    text = "\n".join([f"Rule {index}.\nProvision {index}." for index in range(1, 6000)])
    marker = "See paragraph 2.4 of the reporting Part."
    start = text.index("Provision 4900.")
    text = text[:start] + text[start:].replace("Provision 4900.", marker, 1)
    source = row(text=text)
    candidate_start = text.index("paragraph 2.4")
    candidate = {"candidate_id": "c2", "candidate_text": "paragraph 2.4", "candidate_kind": "structure_reference", "span_start": candidate_start, "span_end": candidate_start + len("paragraph 2.4"), "quoted_text": "paragraph 2.4"}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE edge (from_node_id TEXT,to_node_id TEXT,edge_type TEXT);")
    pack = pass_module.make_context_pack(source, [candidate], {source["id"]: source}, {"exact_title": {}, "structural": {}}, conn, max_tokens=300, neighbour_blocks=2, pass_number=1)
    packed_text = "\n".join(segment["text"] for segment in pack["segments"])
    assert pack["full_source_included"] is False
    assert "paragraph 2.4" in packed_text
    assert pass_module.approx_tokens(packed_text) <= 300


def test_quote_validation_accepts_whitespace_only_difference():
    assert pass_module.quote_in_source("See paragraph\n2.4 in this Part.", "paragraph 2.4") is True
    assert pass_module.quote_in_source("No citation here.", "paragraph 2.4") is False


def test_plural_annex_label_is_canonicalised_for_target_catalogues():
    assert pass_module.canonical_structural_label("annexe") == "annex"
    assert pass_module.canonical_structural_label("paragraph") == "paragraph"


def test_parse_json_output_unwraps_gateway_model_response():
    wrapped = json.dumps({"ok": True, "outputs": [{"text": '{"items":[{"candidate_id":"c1","classification":"ambiguous"}]}'}]})
    assert pass_module.parse_json_output(wrapped)["items"][0]["candidate_id"] == "c1"


def test_normalise_result_items_repairs_single_mistyped_candidate_id():
    parsed = {"items": [{"candidate_id": "typo", "classification": "not_reference"}]}
    items = pass_module.normalise_result_items(parsed, ["expected"])
    assert items == [{"candidate_id": "expected", "classification": "not_reference"}]


def test_normalise_result_items_does_not_guess_partial_response_mappings():
    parsed = {"items": [{"candidate_id": "typo", "classification": "not_reference"}]}
    items = pass_module.normalise_result_items(parsed, ["expected", "also-expected"])
    assert items[0]["candidate_id"] == "typo"
