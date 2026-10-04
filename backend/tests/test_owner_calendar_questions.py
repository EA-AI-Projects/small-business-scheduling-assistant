"""The verified owner can ask read-only calendar questions over SMS (#174)."""

from datetime import UTC, datetime, timedelta
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
from scheduling.domain.conversation_state import InMemoryConversationStates
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.owner_calendar import (
    OwnerAction,
    OwnerCalendarCommand,
    OwnerCalendarService,
)
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)  # Tue Sep 29, 10:00 AM local.
OWNER = "+14155559999"


def local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ZONE).astimezone(UTC)


class Model:
    """A model that must never be needed for a calendar question."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        self.calls.append(body)
        return MessageProposal("clarify", None, None, None, True)


class Consent:
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return None


class Chat:
    def __init__(self) -> None:
        self.store = InMemoryCalendarRepository()
        self.now = NOW
        self.model = Model()
        self.count = 0
        self.names: dict[str, str] = {}
        for client_id, name in (("c1", "Avery Example"), ("c2", "Blake Sample"),
                                ("c3", "Casey Testperson")):
            self.names[client_id] = name
            self.store.save_profile(ClientProfile(
                "pilot", client_id, name, f"+1415555010{client_id[1]}", "1 Test Street",
                HomeSize.MEDIUM, 120, True, 1, NOW, NOW, NOW), 0, None)
        self.service = ConversationService(
            self.store, self.model, HoldService(self.store),
            LifecycleService(self.store, lambda: self.now), Consent(), lambda: self.now,
            OWNER, InMemoryConversationStates())

    def ask(self, body: str) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "pilot", f"SM-{self.count}", OWNER, "+14155550000", body, self.now,
            SenderRole.OWNER, None, Keyword.OTHER, True))

    def hold(self, client: str, start: datetime, key: str, confirm: bool = False) -> str:
        hold_id = HoldService(self.store).create(CreateHold(
            "pilot", client, client, key, start, 120), self.now).hold_id
        if confirm:
            LifecycleService(self.store, lambda: self.now).apply(AppointmentCommand(
                "pilot", hold_id, "owner", ActorRole.OWNER, Action.APPROVE,
                f"ok-{key}", 1))
        return hold_id

    def block(self, start: datetime, end: datetime, key: str) -> None:
        OwnerCalendarService(self.store, lambda: self.now).apply(OwnerCalendarCommand(
            "pilot", "owner", key, OwnerAction.CREATE_BLOCK,
            self.store.read_revision("pilot"), start_at=start, end_at=end))

    def snapshot(self) -> tuple[int, tuple[tuple[str, str], ...]]:
        events = self.store.read_calendar("pilot").events
        return (self.store.read_revision("pilot"),
                tuple(sorted((event.event_id, event.status.value) for event in events)))


def test_tomorrow_lists_clients_with_dates_times_and_each_status() -> None:
    chat = Chat()
    chat.hold("c1", local(9, 30, 9), "a", confirm=True)
    pending = chat.hold("c2", local(9, 30, 13), "b")
    chat.block(local(9, 30, 16), local(9, 30, 17), "blk")
    before = chat.snapshot()

    reply = chat.ask("What clients do I have tomorrow?")

    assert not reply.committed
    assert reply.text.startswith("Wed Sep 30, 2026 (America/Los_Angeles): 1 confirmed visit, "
                                 "1 pending request.")
    assert "Wed Sep 30, 9:00 AM-11:00 AM: Avery Example (confirmed)" in reply.text
    assert f"Wed Sep 30, 1:00 PM-3:00 PM: Blake Sample (pending, ref {pending[:8]})" in reply.text
    assert "unavailable" not in reply.text  # A client list names clients, not blocks.
    assert chat.snapshot() == before
    assert chat.model.calls == []


def test_week_breakdown_marks_empty_days_and_every_status() -> None:
    chat = Chat()
    chat.hold("c1", local(10, 6, 9), "a", confirm=True)
    chat.hold("c2", local(10, 8, 9), "b")
    chat.block(local(10, 9, 12), local(10, 9, 13), "blk")

    reply = chat.ask("What is next week looking like?")

    assert reply.text.startswith(
        "Mon Oct 5 to Sun Oct 11, 2026 (America/Los_Angeles): 1 confirmed visit, "
        "1 pending request, 1 unavailable block.")
    assert "Mon Oct 5: nothing scheduled" in reply.text
    assert "Tue Oct 6, 9:00 AM-11:00 AM: Avery Example (confirmed)" in reply.text
    assert "Thu Oct 8, 9:00 AM-11:00 AM: Blake Sample (pending" in reply.text
    assert "Fri Oct 9, 12:00 PM-1:00 PM: unavailable block" in reply.text
    assert "Sun Oct 11: nothing scheduled" in reply.text


def test_count_states_the_statuses_counted_and_not_counted() -> None:
    chat = Chat()
    chat.hold("c1", local(10, 2, 8), "a", confirm=True)
    chat.hold("c3", local(10, 2, 13), "b", confirm=True)
    chat.hold("c2", local(10, 1, 9), "c")  # Pending, but on Thursday.
    chat.hold("c2", local(10, 2, 10, 30), "d")
    chat.block(local(10, 2, 16), local(10, 2, 17), "blk")

    reply = chat.ask("How many bookings do we have for Friday?")

    assert reply.text == ("Fri Oct 2, 2026 (America/Los_Angeles): 2 confirmed visits counted. "
                          "Not counted: 1 pending request, 1 unavailable block.")
    pending = chat.ask("How many pending requests Friday?")
    assert pending.text.startswith("Fri Oct 2, 2026 (America/Los_Angeles): 1 pending request counted.")
    both = chat.ask("how many bookings including pending on Friday")
    assert "2 confirmed visits, 1 pending request counted." in both.text


def test_empty_period_says_so_without_inventing_items() -> None:
    chat = Chat()
    assert chat.ask("What clients do I have tomorrow?").text == (
        "Wed Sep 30, 2026 (America/Los_Angeles): no confirmed visits or pending requests.")
    assert chat.ask("How many bookings do we have for Friday?").text == (
        "Fri Oct 2, 2026 (America/Los_Angeles): 0 confirmed visits counted.")
    week = chat.ask("What is next week looking like?").text
    assert week.splitlines()[0].endswith("no confirmed visits or pending requests or "
                                         "unavailable blocks.")
    assert week.count("nothing scheduled") == 7


def test_unclear_range_or_status_gets_one_focused_question_then_the_answer() -> None:
    chat = Chat()
    chat.hold("c1", local(10, 2, 9), "a", confirm=True)
    pending = chat.hold("c2", local(10, 2, 13), "b")

    assert chat.ask("How many bookings do we have?").text.startswith(
        "Which day or week do you mean?")
    assert "Fri Oct 2, 2026 (America/Los_Angeles): 1 confirmed visit counted." in \
        chat.ask("Friday").text

    status = chat.ask("How many do we have Friday?").text
    assert status.startswith("For Fri Oct 2, 2026, should I count confirmed visits only")
    # "confirmed" alone is also an approval word; while a count question is open it
    # answers the question and must not approve the pending request.
    answer = chat.ask("confirmed")
    assert not answer.committed
    assert "1 confirmed visit counted" in answer.text
    assert chat.store.read_appointment(pending).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]
    assert chat.model.calls == []


def test_follow_ups_narrow_expand_and_reread_the_current_calendar() -> None:
    chat = Chat()
    chat.hold("c1", local(10, 6, 9), "a", confirm=True)
    chat.hold("c2", local(10, 7, 9), "b")
    first = chat.ask("What is next week looking like?")
    assert "Avery Example" in first.text and "Blake Sample" in first.text

    pending_only = chat.ask("just the pending ones")
    assert "Blake Sample" in pending_only.text and "Avery Example" not in pending_only.text
    assert pending_only.text.startswith("Mon Oct 5 to Sun Oct 11, 2026")

    chat.hold("c3", local(10, 6, 13), "c", confirm=True)  # The calendar changes between turns.
    count = chat.ask("how many?")
    assert count.text.startswith("Mon Oct 5 to Sun Oct 11, 2026 (America/Los_Angeles): "
                                 "1 pending request counted.")
    chat.model.calls.clear()
    everything = chat.ask("what about confirmed")
    assert "2 confirmed visits" in everything.text
    other_day = chat.ask("what about Friday?")
    assert other_day.text.startswith("Fri Oct 2, 2026")
    assert chat.model.calls == []


def test_timezone_boundaries_use_the_business_day_not_utc() -> None:
    chat = Chat()
    # 11:00 PM Thu Oct 1 to 12:30 AM Fri Oct 2 Pacific is entirely Oct 2 in UTC.
    chat.block(local(10, 1, 23), local(10, 2, 0, 30), "late")
    thursday = chat.ask("What do I have on Thursday?")
    friday = chat.ask("What do I have on Friday?")
    saturday = chat.ask("What do I have on Saturday?")
    assert "Thu Oct 1, from 11:00 PM to Fri Oct 2 12:30 AM: unavailable block" in thursday.text
    assert "Fri Oct 2, until 12:30 AM: unavailable block" in friday.text
    assert "no confirmed visits or pending requests or unavailable blocks" in saturday.text
    # The same block counted over a week is one block, not two.
    assert "1 unavailable block." in chat.ask("What is this week looking like?").text.splitlines()[0]
    # Late evening Pacific is already the next UTC day; "tomorrow" follows local time.
    chat.now = datetime(2026, 9, 30, 5, 30, tzinfo=UTC)  # Tue Sep 29, 10:30 PM local.
    assert chat.ask("What clients do I have tomorrow?").text.startswith("Wed Sep 30, 2026")


def test_expired_holds_and_other_statuses_are_not_reported() -> None:
    chat = Chat()
    hold = chat.hold("c2", local(10, 1, 9), "b")
    declined = chat.hold("c1", local(10, 1, 13), "a")
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", declined, "owner", ActorRole.OWNER, Action.DECLINE, "no", 1))
    assert "Blake Sample" in chat.ask("What do I have Thursday?").text
    chat.now += timedelta(days=2)
    chat.now = max(chat.now, chat.store.read_appointment(hold).hold_expires_at + timedelta(minutes=1))  # type: ignore[union-attr]
    text = chat.ask("What do I have on 2026-10-01?").text
    assert "Blake Sample" not in text and "Avery Example" not in text


def test_long_answers_are_paged_never_cut_and_more_continues() -> None:
    chat = Chat()
    for index in range(14):
        chat.block(local(10, 6, 8, 0) + timedelta(minutes=index * 30),
                   local(10, 6, 8, 15) + timedelta(minutes=index * 30), f"b{index}")
    pages = [chat.ask("What is next week looking like?").text]
    assert len(pages[0]) <= 480
    assert "Reply MORE for the rest." in pages[0]
    assert pages[0].splitlines()[-1].startswith("Showing 1-")
    for _ in range(10):
        if "Reply MORE" not in pages[-1]:
            break
        pages.append(chat.ask("more").text)
        assert len(pages[-1]) <= 480
    entries = [line for page in pages for line in page.splitlines()
               if ": unavailable block" in line]
    assert len(entries) == 14 and len(set(entries)) == 14  # Nothing dropped or repeated.
    assert pages[-1].splitlines()[-1].endswith("of 20.")  # 14 blocks + 6 empty days
    assert "nothing more" in chat.ask("more").text


def test_approval_and_decline_still_work_and_questions_change_nothing() -> None:
    chat = Chat()
    request = chat.hold("c1", local(10, 1, 9), "a")
    before = chat.snapshot()
    chat.ask("What is next week looking like?")
    chat.ask("How many bookings do we have for Thursday?")
    assert chat.snapshot() == before
    approved = chat.ask("Yes")
    assert approved.committed and approved.text.startswith("Approved: Avery Example")
    assert chat.store.read_appointment(request).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]
    other = chat.hold("c2", local(10, 5, 9), "b")
    chat.ask("How many do we have next week?")  # Leaves a clarifying question open.
    declined = chat.ask("Decline")
    assert declined.committed
    assert chat.store.read_appointment(other).status == CalendarStatus.DECLINED  # type: ignore[union-attr]


def test_ambiguous_ranges_and_unrelated_texts_are_not_answered_as_calendar_questions() -> None:
    chat = Chat()
    assert "one day or one week at a time" in chat.ask(
        "What do I have tomorrow and Friday?").text
    reply = chat.ask("Please bring up the thermostat settings")
    assert reply.text.startswith("Reply YES to approve")  # Existing owner fallback.
    assert not reply.committed


def test_dynamo_context_round_trips_without_message_text() -> None:
    from datetime import date

    from scheduling.adapters.owner_question_dynamodb import DynamoQuestionContexts
    from scheduling.domain.owner_calendar_questions import Ask, QuestionContext, View

    items: dict[tuple[str, str], dict[str, object]] = {}

    class Client:
        def put_item(self, **kwargs: object) -> dict[str, object]:
            item: dict[str, dict[str, str]] = kwargs["Item"]  # type: ignore[assignment]
            items[(item["PK"]["S"], item["SK"]["S"])] = dict(item)
            return {}

        def get_item(self, **kwargs: object) -> dict[str, object]:
            key: dict[str, dict[str, str]] = kwargs["Key"]  # type: ignore[assignment]
            found = items.get((key["PK"]["S"], key["SK"]["S"]))
            return {"Item": found} if found else {}

    store = DynamoQuestionContexts(Client(), "t")
    context = QuestionContext(
        "pilot", OWNER, View.COUNT, date(2026, 10, 2), date(2026, 10, 2),
        frozenset({CalendarStatus.CONFIRMED}), 3, Ask.STATUS, NOW, NOW + timedelta(minutes=10))
    store.put_context(context)
    assert store.read_context("pilot", OWNER) == context
    assert store.read_context("pilot", "+14155550123") is None
    partial = QuestionContext("pilot", OWNER, View.SUMMARY, None, None, None, 0, Ask.RANGE,
                              NOW, NOW + timedelta(minutes=10))
    store.put_context(partial)
    assert store.read_context("pilot", OWNER) == partial
