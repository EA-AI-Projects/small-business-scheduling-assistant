"""Temporary interpretation of owner calendar and counteroffer texts (#299, #300).

Approval and decline are handled only by the owner tool loop. This proposal can
read a calendar conversation or prepare/answer an open counteroffer, while the
backend still validates the current calendar and offer state.
"""

from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum
from typing import Protocol, runtime_checkable

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.conversation_history import HistoryMessage


class OwnerTransitionIntent(StrEnum):
    CALENDAR_FOLLOWUP = "calendar_followup"
    CALENDAR_QUESTION = "calendar_question"
    PREPARE_COUNTEROFFER = "prepare_counteroffer"
    CONFIRM_OFFER = "confirm_offer"
    CANCEL_OFFER = "cancel_offer"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class PendingRef:
    ref: str
    client: str
    when: str
    version: int = 0


@dataclass(frozen=True)
class OwnerTransitionContext:
    today: date
    timezone: str
    last_kind: str
    view: str
    range_first: date | None
    range_last: date | None
    statuses: tuple[str, ...]
    pending: tuple[PendingRef, ...]
    offer: PendingRef | None = None
    history: tuple[HistoryMessage, ...] = ()


@dataclass(frozen=True)
class OwnerTransitionProposal:
    intent: OwnerTransitionIntent
    request_reference: str | None = None
    statuses: frozenset[CalendarStatus] | None = None
    range_first: date | None = None
    range_last: date | None = None
    request_version: int | None = None
    offer_date: date | None = None
    offer_time: time | None = None


@runtime_checkable
class OwnerTransitionClassifier(Protocol):
    def classify_owner_transition(self, body: str,
                                  context: OwnerTransitionContext) -> OwnerTransitionProposal: ...
