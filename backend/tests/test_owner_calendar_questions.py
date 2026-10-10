"""Owner calendar reads through the #299 model tool.

The scripted model exercises the conversation boundary without a live model or SMS provider.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.conversation import (
    ConversationOutcome,
    ConversationService,
    MessageContext,
    MessageProposal,
)
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.owner_calendar import OwnerAction, OwnerCalendarCommand, OwnerCalendarService
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)
OWNER = "+14155559999"


def local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ZONE).astimezone(UTC)


class Model:
    def __init__(self) -> None:
        self.queries: dict[str, dict[str, Any]] = {}
        self.results: list[dict[str, Any]] = []
        self.histories: list[tuple[HistoryMessage, ...]] = []
        self.calls: list[str] = []
        self.final_text: dict[str, str] = {}

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        raise AssertionError("Owner calendar text must not use client proposal")

    def run_owner_loop(self, body: str, today: date, timezone: str,
                       history: tuple[HistoryMessage, ...], tool: Any) -> str:
        self.calls.append(body)
        self.histories.append(history)
        query = self.queries.get(body)
        if query is None:
            return "Which day or week do you mean?"
        result = tool("get_calendar", query)
        self.results.append(result)
        if body in self.final_text:
            return self.final_text[body]
        if not result["ok"]:
            return "Please choose a shorter date range."
        refs = ", ".join(entry["ref"] for entry in result["entries"])
        return f"Calendar: {result['total']} items; refs {refs or 'none'}."


class History:
    def __init__(self) -> None:
        self.messages: list[HistoryMessage] = []

    def read_conversation_history(self, receipt: InboundReceipt,
                                  now: datetime) -> tuple[HistoryMessage, ...]:
        return tuple(self.messages)


class Chat:
    def __init__(self) -> None:
        self.store = InMemoryCalendarRepository()
        self.now = NOW
        self.model = Model()
        self.history = History()
        self.count = 0
        for client_id, name in (("c1", "Avery Example"), ("c2", "Blake Sample"),
                                ("c3", "Casey Testperson")):
            self.store.save_profile(ClientProfile(
                "pilot", client_id, name, f"+1415555010{client_id[1]}", "1 Test Street",
                HomeSize.MEDIUM, 120, True, 1, NOW, NOW, NOW), 0, None)
        self.service = ConversationService(
            self.store, self.model, HoldService(self.store),
            LifecycleService(self.store, lambda: self.now),
            _Consent(), lambda: self.now, OWNER, history_reader=self.history)

    def query(self, body: str, first: str, last: str, statuses: list[str] | None = None,
              offset: int = 0) -> None:
        self.model.queries[body] = {"from": first, "to": last,
                                    "statuses": statuses or [], "offset": offset}

    def ask(self, body: str) -> ConversationOutcome:
        self.count += 1
        self.now += timedelta(seconds=10)
        receipt = InboundReceipt("pilot", f"SM-{self.count}", OWNER, "+14155550000",
                                 body, self.now, SenderRole.OWNER, None, Keyword.OTHER, True)
        result = self.service.handle(receipt)
        self.history.messages.append(HistoryMessage(receipt.provider_id, "owner", self.now, body))
        self.history.messages.append(HistoryMessage(
            f"SM-reply-{self.count}", "assistant", self.now, result.text))
        return result

    def hold(self, client: str, start: datetime, key: str, confirm: bool = False) -> str:
        hold_id = HoldService(self.store).create(CreateHold(
            "pilot", client, client, key, start, 120), self.now).hold_id
        if confirm:
            LifecycleService(self.store, lambda: self.now).apply(AppointmentCommand(
                "pilot", hold_id, "owner", ActorRole.OWNER, Action.APPROVE, f"ok-{key}", 1))
        return hold_id

    def block(self, start: datetime, end: datetime, key: str) -> None:
        OwnerCalendarService(self.store, lambda: self.now).apply(OwnerCalendarCommand(
            "pilot", "owner", key, OwnerAction.CREATE_BLOCK,
            self.store.read_revision("pilot"), start_at=start, end_at=end))

    def status(self, request: str) -> CalendarStatus:
        found = self.store.read_appointment(request)
        assert found is not None
        return found.status


class _Consent:
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_consent(self, business_id: str, phone_e164: str) -> None:
        return None


def test_day_read_lists_current_appointments_and_excludes_blocks_by_status() -> None:
    chat = Chat()
    confirmed = chat.hold("c1", local(9, 30, 9), "a", confirm=True)
    pending = chat.hold("c2", local(9, 30, 13), "b")
    chat.block(local(9, 30, 16), local(9, 30, 17), "blk")
    chat.query("Who is coming tomorrow?", "2026-09-30", "2026-09-30",
               ["confirmed", "pending"])
    before = chat.store.read_revision("pilot")

    answer = chat.ask("Who is coming tomorrow?")
    result = chat.model.results[-1]
    assert answer.text.startswith("Calendar: 2 items") and not answer.committed
    assert result["timezone"] == "America/Los_Angeles"
    assert result["counts"] == {"confirmed": 1, "pending": 1, "unavailable": 0}
    assert {entry["ref"] for entry in result["entries"]} == {confirmed[:8], pending[:8]}
    assert {entry["client"] for entry in result["entries"]} == {"Avery", "Blake"}
    assert chat.store.read_revision("pilot") == before
    assert chat.status(pending) == CalendarStatus.PENDING_APPROVAL


def test_followup_uses_transcript_but_rereads_changed_calendar() -> None:
    chat = Chat()
    chat.hold("c1", local(10, 6, 9), "a", confirm=True)
    chat.query("How is next week?", "2026-10-05", "2026-10-11")
    assert chat.ask("How is next week?").text.startswith("Calendar: 1 items")
    chat.hold("c2", local(10, 7, 9), "b")
    chat.query("And pending?", "2026-10-05", "2026-10-11", ["pending"])
    answer = chat.ask("And pending?")
    result = chat.model.results[-1]
    assert answer.text.startswith("Calendar: 1 items")
    assert result["counts"] == {"confirmed": 0, "pending": 1, "unavailable": 0}
    assert result["entries"][0]["client"] == "Blake"
    assert "How is next week?" in str(chat.model.histories[-1])
    assert "Calendar: 1 items" in str(chat.model.histories[-1])


def test_paging_uses_explicit_offset_and_fresh_revision() -> None:
    chat = Chat()
    for index in range(10):
        chat.block(local(10, 6, 8) + timedelta(minutes=index * 30),
                   local(10, 6, 8, 15) + timedelta(minutes=index * 30), f"b{index}")
    chat.query("What is next week like?", "2026-10-05", "2026-10-11", ["unavailable"])
    chat.ask("What is next week like?")
    first = chat.model.results[-1]
    assert len(first["entries"]) == 8 and first["next_offset"] == 8
    chat.query("MORE", "2026-10-05", "2026-10-11", ["unavailable"], offset=8)
    chat.ask("MORE")
    second = chat.model.results[-1]
    assert len(second["entries"]) == 2 and second["next_offset"] is None
    assert {item["ref"] for item in first["entries"]}.isdisjoint(
        item["ref"] for item in second["entries"])
    chat.block(local(10, 7, 12), local(10, 7, 13), "new")
    chat.ask("MORE")
    assert chat.model.results[-1]["revision"] > second["revision"]
    assert chat.model.results[-1]["total"] == 11


def test_local_day_and_fall_back_hours_are_preserved() -> None:
    chat = Chat()
    chat.now = datetime(2026, 10, 27, 17, tzinfo=UTC)
    chat.block(datetime(2026, 11, 1, 8, 15, tzinfo=UTC),
               datetime(2026, 11, 1, 8, 30, tzinfo=UTC), "pdt")
    chat.block(datetime(2026, 11, 1, 9, 15, tzinfo=UTC),
               datetime(2026, 11, 1, 9, 30, tzinfo=UTC), "pst")
    chat.query("Sunday blocks?", "2026-11-01", "2026-11-01", ["unavailable"])
    chat.ask("Sunday blocks?")
    entries = chat.model.results[-1]["entries"]
    assert len(entries) == 2
    assert entries[0]["start"].endswith("-07:00")
    assert entries[1]["start"].endswith("-08:00")
    assert entries[0]["start"][:16] == entries[1]["start"][:16]


def test_expired_pending_and_declined_requests_are_not_reported() -> None:
    chat = Chat()
    expired = chat.hold("c2", local(10, 1, 9), "b")
    declined = chat.hold("c1", local(10, 1, 13), "a")
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", declined, "owner", ActorRole.OWNER, Action.DECLINE, "no", 1))
    chat.query("Thursday requests?", "2026-10-01", "2026-10-01", ["pending"])
    assert chat.model.results == []
    chat.ask("Thursday requests?")
    assert chat.model.results[-1]["total"] == 1
    found = chat.store.read_appointment(expired)
    assert found is not None and found.hold_expires_at is not None
    chat.now = found.hold_expires_at + timedelta(minutes=1)
    chat.ask("Thursday requests?")
    assert chat.model.results[-1]["total"] == 0
