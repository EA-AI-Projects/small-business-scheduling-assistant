"""A client answers an owner counteroffer through the model tool loop (#343), scripted.

Boundary: the conversation service, the in-memory calendar, counteroffer store and acceptance
service, hold and lifecycle services, and a scripted model standing in for the model's tool
choices and final text. It proves what the backend acts on, rechecks, writes once, and
checks in the final text, not how the real model reads a yes or no. No live model, SMS, or
AWS is used.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from test_counteroffer_acceptance import World
from test_owner_counteroffer import (
    ASK,
    CLIENT_PHONE,
    NOW,
    OWNER,
    THURSDAY_2PM,
    THURSDAY_9AM,
    Consent,
    History,
    Messages,
    NeverModel,
    Repository,
    profile,
)

from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_tools import check_reply
from scheduling.domain.conversation import ConversationService
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.conversation_state import PromptKind
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.owner_counteroffer import (
    CounterofferAcceptance,
    CounterofferService,
    InMemoryCounterofferStore,
    OfferState,
)
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

Tools = Callable[[str, dict[str, Any]], dict[str, Any]]
SENDER = CLIENT_PHONE
ACCEPTED = ("Requested Thu Oct 1 at 2:00 PM (ref {ref}). It is pending owner approval, not "
            "confirmed yet. Your earlier request for Thu Oct 1 at 9:00 AM stays pending.")


class LoopModel(NeverModel):
    """The owner draft script of the counteroffer tests plus a scripted client loop."""

    def __init__(self) -> None:
        super().__init__()
        self.plan: Callable[[Tools], str] = lambda _tools: "Hello."
        self.loops: list[str] = []
        self.rejected: list[str | None] = []

    def run_client_loop(self, body: str, now: datetime, timezone: str,
                        history: tuple[HistoryMessage, ...], tool: Tools,
                        check: Callable[[str], str | None]) -> str:
        self.loops.append(body)
        text = self.plan(tool)
        self.rejected.append(check(text))
        return text


@pytest.fixture
def world() -> World:
    repository = Repository()
    repository.save_profile(profile("client-1", "Avery Sample", CLIENT_PHONE), 0, None)
    repository.save_profile(profile("client-2", "Blake Example", "+15005550007"), 0, None)
    repository.set_business_profile_for_test("pilot", 120)
    store, consent, clock, history = InMemoryCounterofferStore(), Consent(), [NOW], History()
    holds = HoldService(repository)
    service = ConversationService(
        repository, LoopModel(), holds,
        LifecycleService(repository, lambda: clock[0]), consent, lambda: clock[0], OWNER,
        counteroffers=CounterofferService(repository, consent, store, OWNER),
        counteroffer_acceptance=CounterofferAcceptance(repository, consent, store, holds),
        history_reader=history)
    messages = Messages()
    sender = TwilioSmsSender(messages, repository, consent,  # type: ignore[arg-type]
                             "pilot", "+14155550000", OWNER, clock=lambda: clock[0],
                             counteroffers=store)
    return World(repository, store, consent, service, history, sender, messages, clock)


def model_of(world: World) -> LoopModel:
    model = world.service._interpreter
    assert isinstance(model, LoopModel)
    return model


def offered(world: World) -> str:
    """A pending Thursday 9 AM request with a confirmed 2 PM owner counteroffer; its ID."""
    request = HoldService(world.repository).create(CreateHold(
        "pilot", "client-1", "client-1", "seed", THURSDAY_9AM, 120), NOW).hold_id
    world.owner(ASK)
    assert "Queued the offer" in world.owner("YES").text
    world.clock[0] = NOW + timedelta(minutes=5)
    return request


def only_replacement(world: World, request: str) -> list[Any]:
    return [a for a in world.repository._appointments.values()
            if a.replaces_appointment_id == request]


def test_the_model_reads_only_the_clients_own_open_offer(world: World) -> None:
    offered(world)
    seen: list[dict[str, Any]] = []
    model_of(world).plan = lambda tools: (seen.append(tools("get_my_counteroffer", {})),
                                          ("The owner offered Thu Oct 1 at 2:00 PM. "
                                           "Nothing is requested yet."))[1]
    reply = world.client("what did the owner say?")
    assert model_of(world).rejected == [None] and not reply.committed
    assert seen[0]["open"] is True and seen[0]["when"] == "Thu Oct 1 at 2:00 PM"
    assert seen[0]["your_request"]["when"] == "Thu Oct 1 at 9:00 AM"
    assert seen[0]["owner_approval_required"] is True
    # A different client sees no offer, whatever the model asks.
    world.service.handle(InboundReceipt(
        "pilot", "SM-other", "+15005550007", "+14155550000", "what did the owner say?",
        world.clock[0], SenderRole.CLIENT, "client-2", Keyword.OTHER, True))
    assert seen[1] == {"ok": True, "open": False}


def test_acceptance_in_other_words_creates_a_pending_request_never_a_confirmed_visit(
        world: World) -> None:
    request = offered(world)
    results: list[dict[str, Any]] = []
    model_of(world).plan = lambda tools: (
        results.append(tools("respond_to_counteroffer", {"decision": "accept"})),
        ACCEPTED.format(ref=results[0]["ref"]))[1]
    reply = world.client("Perfect, put me down for that one")
    assert model_of(world).rejected == [None] and reply.committed
    assert results[0]["requested"] is True and results[0]["confirmed"] is False
    (new,) = only_replacement(world, request)
    assert new.status == CalendarStatus.PENDING_APPROVAL and new.start_at == THURSDAY_2PM
    assert world.appointment(request).status == CalendarStatus.PENDING_APPROVAL
    assert not any(a.status == CalendarStatus.CONFIRMED
                   for a in world.repository._appointments.values())
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.ACCEPTED
    # Only the owner's approval confirms it.
    world.lifecycle().apply(AppointmentCommand(
        "pilot", new.appointment_id, OWNER, ActorRole.OWNER, Action.APPROVE, "ok", new.version))
    assert world.appointment(new.appointment_id).status == CalendarStatus.CONFIRMED


def test_declining_books_nothing_and_closes_the_offer(world: World) -> None:
    request = offered(world)
    results: list[dict[str, Any]] = []
    model_of(world).plan = lambda tools: (
        results.append(tools("respond_to_counteroffer", {"decision": "decline"})),
        ("OK, I won't request that time. Nothing was requested and your earlier request is "
         "unchanged."))[1]
    reply = world.client("nope, can't do afternoons")
    assert model_of(world).rejected == [None] and not reply.committed
    assert results[0]["declined"] is True and results[0]["requested"] is False
    assert only_replacement(world, request) == []
    assert world.appointment(request).status == CalendarStatus.PENDING_APPROVAL
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.SUPERSEDED


def test_an_unclear_answer_asks_again_and_changes_nothing(world: World) -> None:
    request = offered(world)
    model_of(world).plan = lambda tools: (
        tools("get_my_counteroffer", {}),
        "Do you want Thu Oct 1 at 2:00 PM? Nothing is requested until you say so.")[1]
    reply = world.client("hmm let me think, maybe")
    assert model_of(world).rejected == [None] and not reply.committed
    assert only_replacement(world, request) == []
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.CONFIRMED


def test_a_request_decided_after_the_offer_is_not_accepted(world: World) -> None:
    request = offered(world)
    world.lifecycle().apply(AppointmentCommand(  # The owner approved the original instead.
        "pilot", request, OWNER, ActorRole.OWNER, Action.APPROVE, "ok", 1))
    results: list[dict[str, Any]] = []
    model_of(world).plan = lambda tools: (
        results.append(tools("respond_to_counteroffer", {"decision": "accept"})),
        "Sorry, that offer is no longer available, so nothing was requested.")[1]
    reply = world.client("Perfect, put me down for that one")
    assert model_of(world).rejected == [None] and not reply.committed
    assert results[0]["requested"] is False
    assert results[0]["reason"] == "original_request_changed"
    assert only_replacement(world, request) == []


def test_a_taken_time_is_refused_without_detail_and_a_bad_text_never_retries(
        world: World) -> None:
    request = offered(world)
    HoldService(world.repository).create(CreateHold(
        "pilot", "client-2", "client-2", "taken", THURSDAY_2PM, 120), NOW)
    results: list[dict[str, Any]] = []
    model_of(world).plan = lambda tools: (
        results.append(tools("respond_to_counteroffer", {"decision": "accept"})),
        "You are all set for 2:00 PM.")[1]  # Not backed by the result: rejected.
    reply = world.client("Perfect, put me down for that one")
    assert model_of(world).rejected[-1] is not None
    assert results[0]["reason"] == "original_request_changed" or results[0]["reason"] \
        == "time_no_longer_open"
    assert "client-2" not in str(results)
    assert "nothing was requested" in reply.text and not reply.committed
    assert only_replacement(world, request) == [] and len(model_of(world).loops) == 1


def test_a_repeated_receipt_and_a_repeated_yes_request_it_once(world: World) -> None:
    request = offered(world)
    results: list[dict[str, Any]] = []
    model_of(world).plan = lambda tools: (
        results.append(tools("respond_to_counteroffer", {"decision": "accept"})),
        ACCEPTED.format(ref=results[-1]["ref"]))[1]
    first = world.client("Perfect, put me down for that one", "SM-same")
    assert first.committed
    # The provider redelivers the same receipt: the offer's own key creates nothing twice.
    model_of(world).plan = lambda tools: (
        results.append(tools("respond_to_counteroffer", {"decision": "accept"})),
        (f"You already asked for that time (ref {results[-1]['ref']}). It is pending owner "
         "approval, so nothing more was requested."))[1]
    again = world.client("Perfect, put me down for that one", "SM-same")
    assert not again.committed and results[-1]["reason"] == "already_requested"
    assert len(only_replacement(world, request)) == 1


def test_a_newer_open_question_blocks_the_answer(world: World) -> None:
    request = offered(world)
    world.service._remember(InboundReceipt(
        "pilot", "SM-x", SENDER, "+14155550000", "x", NOW, SenderRole.CLIENT, "client-1",
        Keyword.OTHER, True), PromptKind.OFFER, world.clock[0], options=(THURSDAY_2PM,),
        duration_minutes=120)
    results: list[dict[str, Any]] = []
    model_of(world).plan = lambda tools: (
        results.append(tools("respond_to_counteroffer", {"decision": "accept"})),
        "Nothing was requested. Which time do you mean?")[1]
    world.client("Perfect, put me down for that one")
    assert results[0]["reason"] == "newer_question_open"
    assert only_replacement(world, request) == []


def test_only_one_action_per_message(world: World) -> None:
    offered(world)
    results: list[dict[str, Any]] = []
    model_of(world).plan = lambda tools: (
        results.append(tools("respond_to_counteroffer", {"decision": "accept"})),
        results.append(tools("request_booking", {"date": "2026-10-01", "time": "14:00"})),
        ACCEPTED.format(ref=results[0]["ref"]))[2]
    reply = world.client("yes, and book me again as well")
    assert reply.committed and results[1] == {"ok": False, "error": "already_requested"}


def test_an_expired_offer_cannot_be_answered(world: World) -> None:
    request = offered(world)
    world.clock[0] = NOW + timedelta(days=2)
    results: list[dict[str, Any]] = []
    model_of(world).plan = lambda tools: (
        results.append(tools("respond_to_counteroffer", {"decision": "accept"})),
        "That offer expired, so nothing was requested.")[1]
    reply = world.client("Perfect, put me down for that one")
    assert not reply.committed and results[0]["reason"] == "offer_expired"
    assert only_replacement(world, request) == []
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.SUPERSEDED


def test_the_reply_must_report_the_real_counteroffer_status() -> None:
    zone = ZoneInfo("America/Los_Angeles")
    done = [{"ok": True, "requested": True, "status": "pending_owner_approval",
             "confirmed": False, "ref": "abcd1234", "date": "2026-10-01", "weekday": "Thursday",
             "time": "2:00 PM", "when": "Thu Oct 1 at 2:00 PM"}]
    good = ("Requested Thu Oct 1 at 2:00 PM (ref abcd1234). It is pending owner approval, "
            "not confirmed yet.")
    assert check_reply(good, done, (), zone) is None
    for bad in ("You're all set for Thu Oct 1 at 2:00 PM (ref abcd1234).",
                ("Your visit is confirmed for Thu Oct 1 at 2:00 PM (ref abcd1234), pending "
                 "owner approval."),
                "Requested Thu Oct 1 at 2:00 PM, pending owner approval."):
        assert check_reply(bad, done, (), zone) is not None, bad
    declined = [{"ok": True, "requested": False, "declined": True, "nothing_changed": True}]
    assert check_reply("I requested that time for you.", declined, (), zone) is not None
    assert check_reply("OK, nothing was requested.", declined, (), zone) is None
