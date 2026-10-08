"""Drafts cannot contradict or rearrange validated scheduling facts."""

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
CANCELLED = ClientReplyResult(
    "cancelled", "cancelled", "safe cancelled text",
    (ClientReplyFact("Thu Oct 1", "9:00 AM", "cancelled", "b1c2d3e4"),),
)
OFFER = ClientReplyResult(
    "offer_made", "none", "safe offer text",
    (ClientReplyFact("Tue Oct 13", "1:00 PM", "open"),),
)
CALENDAR = ClientReplyResult(
    "calendar_list", "mixed", "safe calendar text",
    (ClientReplyFact("Tue Oct 13", "9:00 AM", "confirmed", "a1b2c3d4"),
     ClientReplyFact("Wed Oct 14", "1:00 PM", "pending owner approval", "b1c2d3e4")),
)


@pytest.mark.parametrize(("result", "draft"), [
    (PENDING, "Your request for Tue Oct 13 at 1:00 PM, ref a1b2c3d4, was rejected. "
              + "Pending owner approval."),
    (PENDING, "Your request is confirmed: Tue Oct 13 at 1:00 PM, ref a1b2c3d4, "
              + "pending owner approval."),
    (CANCELLED, "Your Thu Oct 1 at 9:00 AM visit, ref b1c2d3e4, was not cancelled."),
    (OFFER, "No times are open for Tue Oct 13 at 1:00 PM."),
    (CALENDAR, "You have no visits. Tue Oct 13 at 9:00 AM confirmed ref a1b2c3d4; "
               + "Wed Oct 14 at 1:00 PM pending owner approval ref b1c2d3e4."),
    (CALENDAR, "Tue Oct 13 at 1:00 PM confirmed ref a1b2c3d4; "
               + "Wed Oct 14 at 9:00 AM pending owner approval ref b1c2d3e4."),
    (CALENDAR, "Tue Oct 13 at 9:00 AM confirmed ref b1c2d3e4; "
               + "Wed Oct 14 at 1:00 PM pending owner approval ref a1b2c3d4."),
    (CALENDAR, "Tue Oct 13 at 9:00 AM pending owner approval ref a1b2c3d4; "
               + "Wed Oct 14 at 1:00 PM confirmed ref b1c2d3e4."),
    (ClientReplyResult("cancel_kept", "confirmed", "safe kept text",
                       (ClientReplyFact("Thu Oct 1", "9:00 AM", "CONFIRMED", "b1c2d3e4"),)),
     "Thu Oct 1 at 9:00 AM is not confirmed, ref b1c2d3e4. Keep it. Nothing was cancelled."),
])
def test_false_or_swapped_claim_is_rejected(result: ClientReplyResult, draft: str) -> None:
    assert not valid_draft(draft, result)


@pytest.mark.parametrize(("result", "draft"), [
    (PENDING, "Tue Oct 13 at 1:00 PM, ref a1b2c3d4, is pending owner approval."),
    (CANCELLED, "Your Thu Oct 1 at 9:00 AM visit, ref b1c2d3e4, was cancelled."),
    (OFFER, "Tue Oct 13 at 1:00 PM is open. Which time works for you?"),
    (CALENDAR, "Tue Oct 13 at 9:00 AM confirmed ref a1b2c3d4; "
               + "Wed Oct 14 at 1:00 PM pending owner approval ref b1c2d3e4."),
])
def test_natural_truthful_draft_is_accepted(result: ClientReplyResult, draft: str) -> None:
    assert valid_draft(draft, result)


def test_gsm_segment_budget_counts_extensions_and_rejects_nonprintable_text() -> None:
    assert gsm_septets("^" * 80) == 160
    assert gsm_septets("^" * 81) == 162
    assert gsm_septets("`") is None
    assert gsm_septets("\n") is None
    assert not valid_draft("^" * 160, ClientReplyResult("clarification", "none", "safe"))
