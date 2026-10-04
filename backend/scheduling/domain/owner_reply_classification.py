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


# A negator counts only when it sits right before the word it would turn around (within
# NEGATOR_REACH words), so "Don't decline it yet" refuses but "Decline, I can't that day" does
# not. A strong negator right after an approving word also refuses ("yes, not yet"), and a
# contrast word anywhere does ("yes but ...").
NEGATOR_REACH = 3
STRONG_NEGATORS = frozenset({
    "no", "not", "nope", "nah", "don't", "dont", "never", "cant", "can't", "won't", "wont"})
CONTRAST_WORDS = frozenset({"but", "except", "however", "unless"})


def _negated(tokens: list[str], targets: frozenset[str] | set[str]) -> bool:
    """Some target word has a negator in front of it (or a strong one right behind it)."""
    for index, token in enumerate(tokens):
        if token not in targets:
            continue
        if NEGATORS & set(tokens[max(0, index - NEGATOR_REACH):index]):
            return True
        if STRONG_NEGATORS & set(tokens[index + 1:index + 3]):
            return True
    return False


def may_be_approval(body: str) -> bool:
    """True when the reply contains a word that could approve or decline a request."""
    return bool(_words(body) & (APPROVAL_TRIGGERS | {"no", "nope", "nah", "cancel"}))


def supports_approval(body: str) -> bool:
    """An approving word, no decline word, and no negator near it; checked on top of the model."""
    tokens = _tokens(body)
    words = set(tokens)
    approving = words & (APPROVAL_TRIGGERS - DECLINE_WORDS)
    return (bool(approving) and not words & DECLINE_WORDS and not words & CONTRAST_WORDS
            and not _negated(tokens, approving))


def supports_decline(body: str) -> bool:
    """An explicit decline, reject, or deny word that no negator precedes, and no approving
    word anywhere."""
    tokens = _tokens(body)
    words = set(tokens)
    declining = words & DECLINE_WORDS
    if not declining or words & (APPROVAL_TRIGGERS - DECLINE_WORDS):
        return False
    for index, token in enumerate(tokens):
        if token in declining and NEGATORS & set(tokens[max(0, index - NEGATOR_REACH):index]):
            return False
    return True


def supports_offer_send(body: str) -> bool:
    tokens = _tokens(body)
    words = set(tokens)
    sending = words & OFFER_SEND_WORDS
    return (bool(sending) and not words & CONTRAST_WORDS and not _negated(tokens, sending)
            and not words & DECLINE_WORDS)


def supports_offer_cancel(body: str) -> bool:
    return bool(_words(body) & OFFER_CANCEL_WORDS)
