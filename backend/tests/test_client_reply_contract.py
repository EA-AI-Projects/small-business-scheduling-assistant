"""Client SMS drafts keep explicit backend facts within one deliverable segment."""

import pytest

from scheduling.domain.client_replies import (
    ClientReplyFact,
    ClientReplyResult,
    gsm_septets,
    valid_draft,
)

PENDING = ClientReplyResult(
    "request_created", "pending", "safe pending text",
    (ClientReplyFact("Tue Oct 13", "1:00 PM", "pending owner approval", "a1b2c3d4"),),
)
CALENDAR = ClientReplyResult(
    "calendar_list", "mixed", "safe calendar text",
    (ClientReplyFact("Tue Oct 13", "9:00 AM", "confirmed", "a1b2c3d4"),
     ClientReplyFact("Wed Oct 14", "1:00 PM", "pending owner approval", "b1c2d3e4")),
)


@pytest.mark.parametrize("draft", [
    "Your Tue Oct 13 at 1:00 PM request (ref a1b2c3d4) awaits owner approval.",
    "Ref a1b2c3d4: owner approval is pending for 1:00 PM on Tue Oct 13.",
])
def test_grounded_natural_wording_is_accepted(draft: str) -> None:
    assert valid_draft(draft, PENDING)


@pytest.mark.parametrize("draft", [
    "Tue Oct 14 at 1:00 PM, ref a1b2c3d4, awaits owner approval.",
    "Tue Oct 13, 2025 at 1:00 PM, ref a1b2c3d4, awaits owner approval.",
    "Tue Oct 13 at 2:00 PM, ref a1b2c3d4, awaits owner approval.",
    "Tue Oct 13 at 1:00 PM, ref deadbeef, awaits owner approval.",
    "Tue Oct 13 at 1:00 PM, ref a1b2c3d4 or deadbeef, awaits owner approval.",
    "Your request is pending owner approval.",
])
def test_wrong_extra_or_missing_explicit_fact_is_rejected(draft: str) -> None:
    assert not valid_draft(draft, PENDING)


def test_multi_visit_draft_must_name_all_explicit_facts() -> None:
    assert valid_draft(
        "Tue Oct 13 at 9:00 AM confirmed ref a1b2c3d4; "
        "Wed Oct 14 at 1:00 PM pending ref b1c2d3e4.", CALENDAR)
    assert not valid_draft("Tue Oct 13 at 9:00 AM confirmed ref a1b2c3d4.", CALENDAR)


def test_fact_free_clarification_has_no_required_date_time_or_reference() -> None:
    result = ClientReplyResult("clarification", "none", "Which day works for you?")
    assert valid_draft("Which day works for you?", result)
    assert not valid_draft("How about Fri Oct 16 at 2:00 PM?", result)


def test_gsm_segment_budget_counts_extensions_and_rejects_nonprintable_text() -> None:
    assert gsm_septets("^" * 80) == 160
    assert gsm_septets("^" * 81) == 162
    assert gsm_septets("`") is None
    assert gsm_septets("\n") is None
    result = ClientReplyResult("clarification", "none", "safe")
    assert valid_draft("^" * 80, result)
    assert not valid_draft("^" * 81, result)
    assert not valid_draft("`", result)
