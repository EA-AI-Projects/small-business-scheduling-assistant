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


def test_date_only_result_rejects_an_invented_time_or_different_day() -> None:
    result = ClientReplyResult(
        "request_failed", "none", "No openings on Sat Oct 10.",
        (ClientReplyFact("Sat Oct 10", None, "no openings"),))
    assert valid_draft("No openings on Sat Oct 10. Another day?", result)
    assert not valid_draft("No openings on Sat Oct 10 at 9:00 AM.", result)
    assert not valid_draft("No openings on Sun Oct 11.", result)


def test_gsm_segment_budget_counts_extensions_and_rejects_nonprintable_text() -> None:
    assert gsm_septets("^" * 80) == 160
    assert gsm_septets("^" * 81) == 162
    assert gsm_septets("`") is None
    assert gsm_septets("\n") is None
    result = ClientReplyResult("clarification", "none", "safe")
    assert valid_draft("^" * 80, result)
    assert not valid_draft("^" * 81, result)
    assert not valid_draft("`", result)


OWNER_RESULT = ClientReplyResult(
    "owner_calendar", "read_only", "safe", (
        ClientReplyFact("Mon Oct 5, 2026", None, "listed"),
        ClientReplyFact("Mon Oct 5", None, "listed"),
        ClientReplyFact("", "9:00 AM", "listed"),
        ClientReplyFact("", None, "listed", "a1b2c3d4")),
    suffix="\nShowing 1-1 of 2. Reply MORE for the rest.")


def test_owner_draft_keeps_dates_times_and_refs_but_may_drop_the_range_year() -> None:
    from scheduling.domain.client_replies import valid_owner_draft
    assert valid_owner_draft("Mon Oct 5: Avery, 9:00 AM, ref a1b2c3d4.", OWNER_RESULT)
    assert valid_owner_draft("Mon Oct 5, 2026\nAvery 9:00 AM ref a1b2c3d4", OWNER_RESULT)
    for bad in ("Tue Oct 5: Avery, 9:00 AM, ref a1b2c3d4.",
                "Mon Oct 5: Avery, 10:00 AM, ref a1b2c3d4.",
                "Mon Oct 5: Avery, 9:00 AM.",
                "Mon Oct 5: Avery, 9:00 AM, ref a1b2c3d4. Reply MORE.",
                "Mon Oct 5: Avery's visit was approved, ref a1b2c3d4, 9:00 AM.",
                "x" * 460):
        assert not valid_owner_draft(bad, OWNER_RESULT)
    # Only read-only owner kinds are draftable.
    approve = ClientReplyResult("owner_approval", "approved", "safe", OWNER_RESULT.facts)
    assert not valid_owner_draft("Mon Oct 5: Avery, 9:00 AM, ref a1b2c3d4.", approve)
