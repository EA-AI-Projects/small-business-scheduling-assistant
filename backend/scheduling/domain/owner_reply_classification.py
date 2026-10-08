"""Model-proposed meaning of an owner reply (#173, #177).

"yes please" or "confirmed" can mean "approve that request", "keep showing me the
calendar", or "send the offer". The model reads a small bounded context (the last thing
the assistant said, recent pending requests by first name, any open offer) and proposes
one intent. It never approves, declines, or sends anything: the backend acts only after
its own checks (see ``ConversationService._owner_answer``), and every failure, timeout,
or low-confidence answer becomes a clarifying question that changes nothing.

The owner tools (#274) are the same single typed call: ``show_requests`` reads scoped
pending-request details, ``prepare_counteroffer`` names a request (reference and current
version) with a local day and time and only drafts the text the owner must still confirm,
and approve or decline quote the request reference and version, which must be current.
"""

import re
from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum
from typing import Protocol, runtime_checkable

from scheduling.domain.calendar import CalendarStatus

MAX_FOLLOW_UP_DAYS = 31
# A reply with one of these words is never answered as a plain calendar follow-up without the
# model reading it first (routing only; acting is gated by the allowlists below).
APPROVAL_TRIGGERS = frozenset({
    "yes", "y", "yep", "yeah", "ya", "yup", "ok", "okay", "sure", "approve", "approved",
    "confirm", "confirmed", "decline", "declined", "reject", "deny", "accept"})
OFFER_CANCEL_WORDS = frozenset({
    "no", "nope", "nah", "cancel", "never", "mind", "nevermind", "don't", "dont", "stop",
    "discard", "decline", "scrap", "forget"})


class OwnerReplyIntent(StrEnum):
    APPROVE_NAMED_REQUEST = "approve_named_request"
    DECLINE_NAMED_REQUEST = "decline_named_request"
    CALENDAR_FOLLOWUP = "calendar_followup"
    CONFIRM_OFFER = "confirm_offer"  # Send the counteroffer text the owner just reviewed.
    CANCEL_OFFER = "cancel_offer"  # Drop that counteroffer.
    CALENDAR_QUESTION = "calendar_question"  # A calendar question that lacks a day or week.
    HOW_TO = "how_to"  # Asks how to approve, or what the assistant can do.
    # Owner tools (#274). Both name a request by reference and current version.
    SHOW_REQUESTS = "show_requests"  # Read: details of pending requests and any open offer.
    PREPARE_COUNTEROFFER = "prepare_counteroffer"  # Draft an offer; the owner still confirms.
    UNCLEAR = "unclear"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class PendingRef:
    ref: str  # First eight characters of the request ID.
    client: str  # First name only.
    when: str  # Local date and time text.
    version: int = 0  # The stored request version a decision or offer must quote.


@dataclass(frozen=True)
class OwnerReplyContext:
    today: date
    timezone: str
    # "calendar_answer", "approval_question", "offer_prompt", "offer_with_calendar_answer",
    # "offer_closed" (an offer that was cancelled, sent, or lapsed), or "none".
    last_kind: str
    view: str  # "none" when no calendar conversation is open.
    range_first: date | None
    range_last: date | None
    statuses: tuple[str, ...]
    named: PendingRef | None
    pending: tuple[PendingRef, ...]
    offer: PendingRef | None = None  # The offer's request ref and client, with the new time.


@dataclass(frozen=True)
class OwnerReplyProposal:
    intent: OwnerReplyIntent
    request_reference: str | None
    confidence: Confidence
    statuses: frozenset[CalendarStatus] | None = None
    range_first: date | None = None
    range_last: date | None = None
    # The request version the model read from its context; a decision or counteroffer
    # acts only when it equals the stored version (#274).
    request_version: int | None = None
    # The local day and time of a proposed counteroffer (prepare_counteroffer only).
    offer_date: date | None = None
    offer_time: time | None = None


@runtime_checkable
class OwnerReplyClassifier(Protocol):
    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal: ...


# The model may only classify. A proposed approve, decline, or offer send acts only when the
# whole reply, trimmed, lowercased, and without one trailing "." or "!", is exactly one of these
# short replies (optionally followed by "thanks" or "thank you"). Anything longer or hedged
# gets one clarifying question and changes nothing (owner decision, #177). "?" never matches.
APPROVE_REPLIES = frozenset({
    "yes", "y", "yep", "yeah", "yes please", "ok", "okay", "ok please", "sure", "approve",
    "approved", "approve it", "approve please", "confirm", "confirmed", "send it",
    "yes send it"})
# Sending an open offer is not approving a request, so it has its own, smaller list.
SEND_OFFER_REPLIES = frozenset({
    "yes", "y", "yep", "yeah", "yes please", "ok", "okay", "ok please", "sure", "send it",
    "yes send it"})
DECLINE_REPLIES = frozenset({
    "decline", "decline it", "decline please", "please decline", "declined", "reject",
    "reject it", "deny", "deny it"})
_THANKS = re.compile(r"(?: thanks| thank you)$")


def _plain(body: str) -> str:
    text = re.sub(r"\s+", " ", body.strip().lower().replace("\u2019", "'"))
    text = re.sub(r"[.!]$", "", text).strip()  # One trailing mark only.
    return _THANKS.sub("", text)


def _words(body: str) -> set[str]:
    return set(re.findall(r"[a-z']+", body.lower().replace("\u2019", "'")))


def may_be_approval(body: str) -> bool:
    """True when the reply contains a word that could approve or decline a request."""
    return bool(_words(body) & (APPROVAL_TRIGGERS | {"no", "nope", "nah", "cancel"}))


def supports_approval(body: str) -> bool:
    return _plain(body) in APPROVE_REPLIES


def supports_decline(body: str) -> bool:
    return _plain(body) in DECLINE_REPLIES


def supports_offer_send(body: str) -> bool:
    return _plain(body) in SEND_OFFER_REPLIES


def supports_offer_cancel(body: str) -> bool:
    return bool(_words(body) & OFFER_CANCEL_WORDS)
