from __future__ import annotations

from scripts.stage_legal_context_references import candidate_windows


def test_candidate_windows_keep_candidates_inside_bounded_context() -> None:
    windows = candidate_windows(
        [(100, 140, "a"), (180, 220, "b"), (4_900, 4_940, "c")],
        text_length=5_000,
        padding=100,
        max_chars=500,
    )

    assert windows == [(0, 320), (4_800, 5_000)]
    for start, end, _candidate_id in [(100, 140, "a"), (180, 220, "b"), (4_900, 4_940, "c")]:
        assert any(window_start <= start and end <= window_end for window_start, window_end in windows)
