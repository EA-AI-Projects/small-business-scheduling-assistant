"""Model-proposed meaning of an owner reply while a calendar conversation is open.

After a calendar answer, "yes please" or "confirmed" can mean "approve that request" or
"keep showing me the calendar". The model reads a small bounded context and proposes
one of four intents. It never approves anything: the backend acts on an approval only
when the proposed reference is the request the assistant last named, that request is
still the single pending one at the same version, and the model was confident.
"""

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable

from scheduling.domain.calendar import CalendarStatus

MAX_FOLLOW_UP_DAYS = 31
# A reply that contains one of these words may be an approval, so it goes to the model.
APPROVAL_TRIGGERS = frozenset({
    "yes", "y", "yep", "yeah", "ya", "yup", "ok", "okay", "sure", "approve", "approved",
    "confirm", "confirmed", "decline", "declined", "reject", "deny", "accept"})


class OwnerReplyIntent(StrEnum):
    APPROVE_NAMED_REQUEST = "approve_named_request"
    DECLINE_NAMED_REQUEST = "decline_named_request"
    CALENDAR_FOLLOWUP = "calendar_followup"
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
    last_kind: str  # "calendar_answer" or "approval_question"
    view: str
    range_first: date | None
    range_last: date | None
    statuses: tuple[str, ...]
    named: PendingRef | None
    pending: tuple[PendingRef, ...]


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


def may_be_approval(body: str) -> bool:
    """True when the reply contains a word that could approve or decline a request."""
    words = re.findall(r"[a-z']+", body.lower())
    return any(word in APPROVAL_TRIGGERS for word in words)
