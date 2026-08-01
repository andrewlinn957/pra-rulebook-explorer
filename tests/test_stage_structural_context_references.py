from scripts.stage_structural_context_references import repair_structural_span


def test_repairs_concatenated_second_structural_label() -> None:
    value = (
        "Rules 3.2 and 3.4 of the Operational Continuity Part of the PRA Rulebook "
        "Rules 2.2"
    )
    repaired, start, end = repair_structural_span(value)
    assert (start, end) == (0, len(repaired))
    assert repaired.endswith("PRA Rulebook")
    assert "Rules 2.2" not in repaired


def test_repairs_dangling_connector_and_keeps_schedule_context() -> None:
    repaired, _start, _end = repair_structural_span("paragraph 9 of Part II of")
    assert repaired == "paragraph 9 of Part II"

    repaired, _start, _end = repair_structural_span(
        "paragraphs 29(2) and 36 of Schedule 2) and have attained the age"
    )
    assert repaired == "paragraphs 29(2) and 36 of Schedule 2"
