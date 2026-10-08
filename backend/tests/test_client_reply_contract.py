"""Client SMS drafts keep explicit backend facts within one deliverable segment."""

import pytest

from scheduling.domain.client_replies import (
    ClientReplyFact,
    ClientReplyResult,
    facts_from_text,
    gsm_septets,
    valid_draft,
    valid_owner_draft,
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


BACKEND = ("Mon Oct 5 to Sun Oct 11, 2026: 2 pending requests.\n"
           "Mon Oct 5, 9:00 AM-11:00 AM: Avery (pending, ref a1b2c3d4)\n"
           "Tue Oct 6, 1:00 PM-3:00 PM: Blake (pending, ref b2c3d4e5)")
OWNER_RESULT = ClientReplyResult(
    "owner_calendar", "read_only", "safe", facts_from_text(BACKEND), None, BACKEND,
    ("MORE shows the next page",))
GOOD = ("From Mon Oct 5 to Sun Oct 11 there are two requests. Avery is on Mon Oct 5 from 9:00 AM to 11:00 AM "
        "(ref a1b2c3d4). Blake is on Tue Oct 6 from 1:00 PM to 3:00 PM (ref b2c3d4e5).")


def test_owner_draft_per_entry_and_year_checks() -> None:
    assert valid_owner_draft(GOOD, OWNER_RESULT)
    assert valid_owner_draft(GOOD.replace("Sun Oct 11 there", "Sun Oct 11, 2026 there"),
                             OWNER_RESULT)
    for bad in (
            GOOD.replace("ref a1b2c3d4", "ref TMP").replace("ref b2c3d4e5", "ref a1b2c3d4")
            .replace("ref TMP", "ref b2c3d4e5"),                       # Refs swapped.
            GOOD.replace("Avery is on Mon Oct 5", "Avery is on Tue Oct 6"),  # Dates swapped.
            GOOD.replace("Sun Oct 11 there", "Sun Oct 11, 2027 there"),
            GOOD + " Also Wed Oct 7.", GOOD.replace("3:00 PM", "4:00 PM"),
            GOOD.replace(" (ref b2c3d4e5)", ""),
            ):
        assert not valid_owner_draft(bad, OWNER_RESULT), bad
    # Only read-only owner kinds are draftable.
    approve = ClientReplyResult("owner_approval", "approved", "safe", OWNER_RESULT.facts,
                                None, BACKEND)
    assert not valid_owner_draft(GOOD, approve)


PAIR = ("2 requests are pending: Avery, Thu Oct 1 at 9:00 AM (ref a1b2c3d4); "
        "Blake, Fri Oct 2 at 1:00 PM (ref b2c3d4e5).")
PAIR_RESULT = ClientReplyResult("owner_requests", "read_only", "safe",
                                facts_from_text(PAIR), None, PAIR)


@pytest.mark.parametrize("draft", [
    ("Avery is on Thu Oct 1 at 9:00 AM (ref a1b2c3d4) and Blake on Fri Oct 2 at 1:00 PM "
    "(ref b2c3d4e5)."),
    ("Avery, Thu Oct 1 at 9:00 AM, ref a1b2c3d4, and Blake, Fri Oct 2 at 1:00 PM, "
    "ref b2c3d4e5."),
    ("Two are waiting. Avery (ref a1b2c3d4) is Thu Oct 1 at 9:00 AM. Blake (ref b2c3d4e5) "
    "is Fri Oct 2 at 1:00 PM."),
])
def test_natural_two_entry_drafts_are_accepted(draft: str) -> None:
    assert valid_owner_draft(draft, PAIR_RESULT)


@pytest.mark.parametrize("draft", [
    ("Avery is on Thu Oct 1 at 9:00 AM (ref b2c3d4e5) and Blake on Fri Oct 2 at 1:00 PM "
    "(ref a1b2c3d4)."),
    ("Avery, Thu Oct 1 at 9:00 AM, ref b2c3d4e5, and Blake, Fri Oct 2 at 1:00 PM, "
    "ref a1b2c3d4."),
    ("Avery is on Fri Oct 2 at 1:00 PM (ref a1b2c3d4) and Blake on Thu Oct 1 at 9:00 AM "
    "(ref b2c3d4e5)."),
    # Restated in a sentence that carries no reference.
    ("Avery (ref a1b2c3d4) is Thu Oct 1 at 9:00 AM. Blake (ref b2c3d4e5) is Fri Oct 2 at "
    "1:00 PM. Correction: Avery is at 1:00 PM on Fri Oct 2."),
])
def test_swapped_two_entry_drafts_are_refused(draft: str) -> None:
    assert not valid_owner_draft(draft, PAIR_RESULT)


def test_instructions_and_prompts_are_not_filtered() -> None:
    # Owner decision on #285: the model writes the instructions; only facts are checked.
    good = ("Avery is on Thu Oct 1 at 9:00 AM (ref a1b2c3d4) and Blake on Fri Oct 2 at "
            "1:00 PM (ref b2c3d4e5). ")
    for text in ("Reply APPROVE a1b2c3d4 or DECLINE a1b2c3d4. Reply YES to send the offer.",
                 "Nothing was approved or sent. MORE shows the rest."):
        assert valid_owner_draft(good + text, PAIR_RESULT)


def test_open_offer_facts_may_be_mentioned_but_need_not_be() -> None:
    offer = facts_from_text("Sat Oct 3 at 2:00 PM a1b2c3d4")
    result = ClientReplyResult("owner_requests", "read_only", "safe", PAIR_RESULT.facts,
                               None, PAIR, ("YES sends the open offer",), offer)
    plain = ("Avery is on Thu Oct 1 at 9:00 AM (ref a1b2c3d4) and Blake on Fri Oct 2 at "
             "1:00 PM (ref b2c3d4e5).")
    assert valid_owner_draft(plain, result)
    assert valid_owner_draft(plain + " Your offer for Sat Oct 3 at 2:00 PM still waits "
                             "for YES or NO. Or APPROVE a1b2c3d4.", result)
    assert not valid_owner_draft(plain + " Your offer for Sun Oct 4 at 2:00 PM waits.", result)
    assert not valid_owner_draft(plain + " Your offer for Sat Oct 3 at 3:00 PM waits.", result)


def test_ordinary_lowercase_no_and_more_are_allowed() -> None:
    good = ("Avery is on Thu Oct 1 at 9:00 AM (ref a1b2c3d4) and Blake on Fri Oct 2 at "
            "1:00 PM (ref b2c3d4e5). ")
    assert valid_owner_draft(good + "There are no other pending requests, and a few more "
                             "may follow.", PAIR_RESULT)
