import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "recover_stale_reference_sources.py"
spec = importlib.util.spec_from_file_location("recover_stale_reference_sources", SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_text_contains_accepts_whitespace_variants():
    assert module.text_contains("See paragraph\n4.2(c) in this Part.", "paragraph 4.2(c)") is True


def test_text_contains_rejects_missing_phrase():
    assert module.text_contains("See paragraph 4.2(c).", "paragraph 4.3") is False
