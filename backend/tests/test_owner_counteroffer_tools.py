"""Scripted owner counteroffer tools; no model API or SMS provider calls."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from test_sms_processing import Reader

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot, CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.conversation import ConversationService, MessageContext, MessageProposal
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.owner_counteroffer import (
    COUNTEROFFER_TEMPLATE,
    CounterofferService,
    InMemoryCounterofferStore,
    OfferState,
)
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)
THURSDAY_9AM = datetime(2026, 10, 1, 16, tzinfo=UTC)
OWNER = "+15005550009"
CLIENT_PHONE = "+15005550006"
CLIENT_TEXT = "Could Thu Oct 1 at 2:00 PM work? Reply YES to request it. Approval is still needed."
DAY = date(2026, 10, 1)


def profile(client_id: str, name: str, phone: str) -> ClientProfile:
    return ClientProfile("pilot", client_id, name, phone, "123 Test Street", HomeSize.MEDIUM,
                         120, True, 1, NOW, NOW, NOW)


class Repository(InMemoryCalendarRepository):
    blocked_start: datetime | None = None

    def read_calendar(self, business_id: str) -> CalendarSnapshot:
        calendar = super().read_calendar(business_id)
        if self.blocked_start is None:
            return calendar
        block = CalendarEvent("blocked", self.blocked_start,
                              self.blocked_start + timedelta(hours=2),
                              CalendarStatus.UNAVAILABLE)
        return replace(calendar, events=calendar.events + (block,))


class Consent:
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        if phone_e164 == CLIENT_PHONE:
            return ConsentEvidence(business_id, "client-1", "Avery Sample", phone_e164, NOW, "v1")
        if phone_e164 == "+15005550007":
            return ConsentEvidence(business_id, "client-2", "Blake Example", phone_e164, NOW, "v1")
        return None


class Model:
    def __init__(self) -> None:
        self.actions: dict[str, Callable[[Callable[[str, dict[str, Any]], dict[str, Any]]], str]] = {}
        self.results: list[dict[str, Any]] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        raise AssertionError("Owner messages must use the tool loop")

    def run_owner_loop(self, body: str, today: date, timezone: str,
                       history: tuple[HistoryMessage, ...],
                       tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        def captured(name: str, args: dict[str, Any]) -> dict[str, Any]:
            result = tool(name, args)
            self.results.append(result)
            return result
        return self.actions[body](captured)


class World:
    def __init__(self) -> None:
        self.repository = Repository()
        self.repository.save_profile(profile("client-1", "Avery Sample", CLIENT_PHONE), 0, None)
        self.repository.save_profile(profile("client-2", "Blake Example", "+15005550007"),
                                     0, None)
        self.consent = Consent()
        self.store = InMemoryCounterofferStore()
        self.model = Model()
        self.now = NOW
        self.service = ConversationService(
            self.repository, self.model, HoldService(self.repository),
            LifecycleService(self.repository, lambda: self.now), self.consent,
            lambda: self.now, OWNER,
            counteroffers=CounterofferService(self.repository, self.consent, self.store, OWNER))

    def pending(self, key: str = "first", client: str = "client-1",
                start: datetime = THURSDAY_9AM) -> str:
        return HoldService(self.repository).create(CreateHold(
            "pilot", client, client, key, start, 120), self.now).hold_id

    def owner(self, body: str, provider_id: str) -> str:
        receipt = InboundReceipt("pilot", provider_id, OWNER, "+14155550000", body,
                                 self.now, SenderRole.OWNER, None, Keyword.OTHER, True)
        return self.service.handle(receipt).text


def draft(tool: Callable[[str, dict[str, Any]], dict[str, Any]],
          client_text: str = CLIENT_TEXT, ref: str | None = None,
          day: str = "2026-10-01", clock: str = "14:00") -> dict[str, Any]:
    listed = tool("list_pending_requests", {})["requests"]
    request = listed[0]
    return tool("draft_counteroffer", {"ref": ref or request["ref"],
                                       "version": request["version"], "date": day,
                                       "time": clock, "client_text": client_text})


def test_draft_then_plain_yes_queues_exact_model_text_once() -> None:
    world = World()
    request = world.pending()
    world.model.actions["Offer 2 PM"] = lambda tool: (
        "Drafted; please confirm this exact client text: " + draft(tool)["client_text"])
    assert CLIENT_TEXT in world.owner("Offer 2 PM", "SM-draft")
    proposed = world.store.read_active("pilot", OWNER)
    assert proposed is not None and proposed.state == OfferState.PROPOSED
    assert proposed.text == CLIENT_TEXT and not world.store.outbox

    def send(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        opened = tool("list_pending_requests", {})["open_offer"]
        assert opened["client_text"] == CLIENT_TEXT
        result = tool("send_counteroffer", {"draft_id": opened["draft_id"]})
        assert result["ok"] and result["queued"]
        return "Queued the stored text for Avery; the request remains pending."

    world.model.actions["yes"] = send
    assert "Queued" in world.owner("yes", "SM-send")
    assert world.repository.read_appointment(request).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]
    assert proposed.offer_id in world.store.outbox[f"counteroffer#{proposed.offer_id}"].outbox_id
    assert world.store.outbox[f"counteroffer#{proposed.offer_id}"].template == COUNTEROFFER_TEMPLATE
    confirmed = world.store.read("pilot", proposed.offer_id)
    assert confirmed is not None and confirmed.text == CLIENT_TEXT
    assert confirmed.state == OfferState.CONFIRMED
    assert world.store.read_confirmed_for_client("pilot", "client-1") == confirmed
    assert len(world.store.outbox) == 1


def test_cancel_and_revised_instruction_change_draft_without_client_send() -> None:
    world = World()
    world.pending()
    world.model.actions["first"] = lambda tool: "Drafted." if draft(tool)["ok"] else "Failed."
    world.owner("first", "SM-first")
    first = world.store.read_active("pilot", OWNER)
    assert first is not None
    revised = "Would Thu Oct 1 at 3:00 PM work? Reply YES to request it."
    world.model.actions["revise"] = lambda tool: (
        "Revised." if draft(tool, revised, clock="15:00")["ok"] else "Failed.")
    world.owner("revise", "SM-revise")
    second = world.store.read_active("pilot", OWNER)
    assert second is not None and second.offer_id != first.offer_id
    assert second.text == revised
    assert world.store.read("pilot", first.offer_id).state == OfferState.DISCARDED  # type: ignore[union-attr]

    def cancel(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        opened = tool("list_pending_requests", {})["open_offer"]
        assert tool("cancel_counteroffer", {"draft_id": opened["draft_id"]})["cancelled"]
        return "Cancelled; nothing was sent."

    world.model.actions["no"] = cancel
    assert "nothing was sent" in world.owner("no", "SM-cancel")
    assert world.store.read("pilot", second.offer_id).state == OfferState.DISCARDED  # type: ignore[union-attr]
    assert not world.store.outbox


def test_owner_approval_can_supersede_open_draft_without_sending_it() -> None:
    world = World()
    request = world.pending()
    world.model.actions["draft"] = lambda tool: "Drafted." if draft(tool)["ok"] else "Failed."
    world.owner("draft", "SM-draft")
    offer = world.store.read_active("pilot", OWNER)
    assert offer is not None

    def approve(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        listed = tool("list_pending_requests", {})["requests"][0]
        result = tool("approve_request", {"ref": listed["ref"],
                                          "version": listed["version"]})
        assert result["ok"]
        return "Approved the original request."

    world.model.actions["Approve it"] = approve
    assert world.owner("Approve it", "SM-approve") == "Approved the original request."
    assert world.repository.read_appointment(request).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]
    assert world.store.read("pilot", offer.offer_id).state == OfferState.DISCARDED  # type: ignore[union-attr]
    assert not world.store.outbox


def test_send_reply_failure_and_receipt_redelivery_never_queue_second_client_text() -> None:
    world = World()
    world.pending()
    world.model.actions["draft"] = lambda tool: "Drafted." if draft(tool)["ok"] else "Failed."
    world.owner("draft", "SM-draft")
    offer = world.store.read_active("pilot", OWNER)
    assert offer is not None

    def send_and_fail(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        assert tool("send_counteroffer", {"draft_id": offer.offer_id})["ok"]
        raise RuntimeError("model final turn failed")

    world.model.actions["yes"] = send_and_fail
    inbound = InboundReceipt("pilot", "SM-send", OWNER, "+14155550000", "yes", world.now,
                             SenderRole.OWNER, None, Keyword.OTHER, True)
    reader = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(reader, world.service, "pilot", lambda: world.now)
    assert processor.process("pilot", "SM-send").text == "Something went wrong. Please try again."  # type: ignore[union-attr]
    assert processor.process("pilot", "SM-send") is None
    assert reader.replies == [("SM-send", "Something went wrong. Please try again.")]
    assert len(world.store.outbox) == 1
    assert world.store.read("pilot", offer.offer_id).confirmed_by == "SM-send"  # type: ignore[union-attr]


def test_redelivered_owner_yes_reuses_stored_reply_and_one_client_outbox() -> None:
    world = World()
    world.pending()
    world.model.actions["draft"] = lambda tool: "Drafted." if draft(tool)["ok"] else "Failed."
    world.owner("draft", "SM-draft")
    offer = world.store.read_active("pilot", OWNER)
    assert offer is not None
    calls = 0

    def send(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        nonlocal calls
        calls += 1
        assert tool("send_counteroffer", {"draft_id": offer.offer_id})["ok"]
        return "Queued Avery's offer once."

    world.model.actions["yes"] = send
    inbound = InboundReceipt("pilot", "SM-send", OWNER, "+14155550000", "yes", world.now,
                             SenderRole.OWNER, None, Keyword.OTHER, True)
    reader = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(reader, world.service, "pilot", lambda: world.now)
    assert processor.process("pilot", "SM-send").text == "Queued Avery's offer once."  # type: ignore[union-attr]
    assert processor.process("pilot", "SM-send") is None
    assert reader.replies == [("SM-send", "Queued Avery's offer once.")]
    assert calls == 1 and len(world.store.outbox) == 1


@pytest.mark.parametrize("failure", ["expired", "newer_request", "client_ineligible",
                                      "slot_unavailable"])
def test_send_rechecks_and_returns_error_without_outbox(failure: str) -> None:
    world = World()
    world.pending()
    world.model.actions["first"] = lambda tool: "Drafted." if draft(tool)["ok"] else "Failed."
    world.owner("first", "SM-first")
    offer = world.store.read_active("pilot", OWNER)
    assert offer is not None
    if failure == "expired":
        world.now += timedelta(minutes=31)
    elif failure == "newer_request":
        world.now += timedelta(minutes=1)
        world.pending("newer", "client-2", THURSDAY_9AM + timedelta(days=1))
    elif failure == "client_ineligible":
        current = world.repository.read_profile("pilot", "client-1")
        assert current is not None
        world.repository.save_profile(replace(current, active=False), current.version,
                                      current.phone_e164)
    else:
        world.repository.blocked_start = offer.proposed_start

    world.model.actions["send"] = lambda tool: (
        "Nothing was sent." if not tool("send_counteroffer", {"draft_id": offer.offer_id})["ok"]
        else "Unexpected send.")
    assert world.owner("send", "SM-send") == "Nothing was sent."
    assert world.model.results[-1]["error"] == failure
    assert not world.store.outbox
