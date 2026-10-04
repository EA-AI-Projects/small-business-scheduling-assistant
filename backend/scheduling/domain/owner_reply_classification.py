"""Model-proposed meaning of an owner reply (#173, #177).

"yes please" or "confirmed" can mean "approve that request", "keep showing me the
calendar", or "send the offer". The model reads a small bounded context (the last thing
the assistant said, recent pending requests by first name, any open offer) and proposes
one intent. It never approves, declines, or sends anything: the backend acts only after
its own checks (see ``ConversationService._owner_answer``), and every failure, timeout,
or low-confidence answer becomes a clarifying question that changes nothing.
"""

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable

from scheduling.domain.calendar import CalendarStatus

MAX_FOLLOW_UP_DAYS = 31
# Defence in depth, not routing: the backend refuses a model approval or decline whose
# text has none of the matching words, and never answers a reply that has one of these
# words as a plain calendar follow-up without the model reading it first.
APPROVAL_TRIGGERS = frozenset({
    "yes", "y", "yep", "yeah", "ya", "yup", "ok", "okay", "sure", "approve", "approved",
    "confirm", "confirmed", "decline", "declined", "reject", "deny", "accept"})
# A request is declined by the model only on one of these explicit words. A bare "no",
# "nope", "nah", or "cancel" never declines a request (PRD 6.3 names only "decline").
DECLINE_WORDS = frozenset({"decline", "declined", "reject", "rejected", "deny", "denied"})
NEGATION_WORDS = frozenset({
    "no", "not", "nope", "nah", "don't", "dont", "never", "cant", "can't", "won't", "wont",
    "wait", "hold", "stop", "cancel", "decline", "declined", "reject", "rejected", "deny",
    "denied"})
# Negators that can turn a decline word around ("don't decline it", "no, do not decline").
NEGATORS = NEGATION_WORDS - DECLINE_WORDS
OFFER_SEND_WORDS = APPROVAL_TRIGGERS | frozenset({"go", "ahead", "send"})
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


@runtime_checkable
class OwnerReplyClassifier(Protocol):
    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal: ...


def _tokens(body: str) -> list[str]:
    return re.findall(r"[a-z']+", body.lower().replace("\u2019", "'"))


def _words(body: str) -> set[str]:
    return set(_tokens(body))


# Fail closed: a model verdict is trusted only when the text does not retract itself.
# - Before the word it would reverse, a negator within NEGATOR_REACH words refuses.
# - After it, any retraction marker anywhere later refuses ("ok actually hold off", "decline,
#   wait", "yes, I mean no", "decline it. actually don't"). Contrast words refuse too.
# - Approving or sending also refuses a marker anywhere before it, so "yes, hold it for her"
#   and "yes send it, I'll wait" ask instead (a known limit, see CONVERSATION.md).
# - The only reasons allowed after a decline word are masked below by exact phrase.
NEGATOR_REACH = 3
RETRACTION_MARKERS = frozenset({
    "no", "nope", "nah", "not", "wait", "hold", "stop", "cancel", "never", "mind",
    "nevermind", "actually", "scratch", "yet", "don't", "dont", "won't", "wont", "can't",
    "cant", "cancelled", "canceled"})
CONTRAST_WORDS = frozenset({"but", "except", "however", "unless"})
DECLINE_REASONS = re.compile(
    r"\b(?:i )?can't\b|\bwon't work\b|\bnot available\b|\bunavailable\b")


def _text(body: str) -> str:
    return body.lower().replace("\u2019", "'")


def _retracts(tokens: list[str]) -> bool:
    words = set(tokens)
    return bool(words & (RETRACTION_MARKERS | CONTRAST_WORDS))


def may_be_approval(body: str) -> bool:
    """True when the reply contains a word that could approve or decline a request."""
    return bool(_words(body) & (APPROVAL_TRIGGERS | {"no", "nope", "nah", "cancel"}))


def supports_approval(body: str) -> bool:
    """An approving word, no decline word, and no retraction anywhere in the text."""
    tokens = _tokens(body)
    words = set(tokens)
    return (bool(words & (APPROVAL_TRIGGERS - DECLINE_WORDS)) and not words & DECLINE_WORDS
            and not _retracts(tokens))


def supports_decline(body: str) -> bool:
    """An explicit decline, reject, or deny word, no approving word, no negator just before it,
    and no retraction after it (except the exact reasons in ``DECLINE_REASONS``)."""
    text = _text(body)
    tokens = _tokens(body)
    words = set(tokens)
    if not words & DECLINE_WORDS or words & (APPROVAL_TRIGGERS - DECLINE_WORDS):
        return False
    first = next(index for index, token in enumerate(tokens) if token in DECLINE_WORDS)
    if RETRACTION_MARKERS & set(tokens[max(0, first - NEGATOR_REACH):first]):
        return False
    position = min((match.start() for match in re.finditer(r"[a-z']+", text)
                    if match.group() in DECLINE_WORDS), default=0)
    after = DECLINE_REASONS.sub(" ", text[position:])
    return not _retracts(re.findall(r"[a-z']+", after)[1:])


def supports_offer_send(body: str) -> bool:
    tokens = _tokens(body)
    words = set(tokens)
    return (bool(words & OFFER_SEND_WORDS) and not words & DECLINE_WORDS
            and not _retracts(tokens))


def supports_offer_cancel(body: str) -> bool:
    return bool(_words(body) & OFFER_CANCEL_WORDS)
