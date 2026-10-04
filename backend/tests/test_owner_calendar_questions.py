"""The verified owner can ask read-only calendar questions over SMS (#174)."""

from datetime import UTC, date, datetime, timedelta
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
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyContext,
    OwnerReplyIntent,
    OwnerReplyProposal,
)
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)  # Tue Sep 29, 10:00 AM local.
OWNER = "+14155559999"


def local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ZONE).astimezone(UTC)


UNCLEAR = OwnerReplyProposal(OwnerReplyIntent.UNCLEAR, None, Confidence.HIGH)


class Model:
    """A scripted fake model. Calendar questions never need it; approval-like replies do."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.classified: list[tuple[str, OwnerReplyContext]] = []
        self.script: dict[str, OwnerReplyProposal] = {}
        self.error: Exception | None = None

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        self.calls.append(body)
        return MessageProposal("clarify", None, None, None, True)

    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal:
        self.classified.append((body, context))
        if self.error is not None:
            raise self.error
        return self.script.get(body, UNCLEAR)


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

    def ask(self, body: str, provider_id: str | None = None) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "pilot", provider_id or f"SM-{self.count}", OWNER, "+14155550000", body, self.now,
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

    # A bare "bookings" count does not say which statuses; it asks instead of choosing.
    asked = chat.ask("How many bookings do we have for Friday?")
    assert asked.text.startswith("For Fri Oct 2, 2026, should I count confirmed visits only")
    reply = chat.ask("confirmed")

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
    chat.ask("How many bookings do we have for Friday?")
    assert chat.ask("both").text == (
        "Fri Oct 2, 2026 (America/Los_Angeles): 0 confirmed visits, 0 pending requests counted.")
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
    assert chat.ask("Friday").text.startswith("For Fri Oct 2, 2026, should I count")
    assert "1 confirmed visit counted." in chat.ask("confirmed").text

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
    chat.model.script["what about confirmed"] = OwnerReplyProposal(
        OwnerReplyIntent.CALENDAR_FOLLOWUP, None, Confidence.HIGH,
        frozenset({CalendarStatus.CONFIRMED}))
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
        frozenset({CalendarStatus.CONFIRMED}), 3, Ask.STATUS, NOW, NOW + timedelta(minutes=10),
        2, "SM-1", "abc", "SM-2", 4)
    store.put_context(context)
    assert store.read_context("pilot", OWNER) == context
    assert store.read_context("pilot", "+14155550123") is None
    partial = QuestionContext("pilot", OWNER, View.SUMMARY, None, None, None, 0, Ask.RANGE,
                              NOW, NOW + timedelta(minutes=10))
    store.put_context(partial)
    assert store.read_context("pilot", OWNER) == partial


def week_with_one_pending() -> tuple[Chat, str]:
    chat = Chat()
    chat.hold("c1", local(10, 6, 9), "a", confirm=True)
    request = chat.hold("c2", local(10, 7, 9), "b")
    chat.ask("What is next week looking like?")
    return chat, request


def status_of(chat: Chat, request: str) -> CalendarStatus:
    appointment = chat.store.read_appointment(request)
    assert appointment is not None
    return appointment.status


def decision(intent: OwnerReplyIntent, request: str | None,
             confidence: Confidence = Confidence.HIGH) -> OwnerReplyProposal:
    return OwnerReplyProposal(intent, request[:8] if request else None, confidence)


def only_confirmed() -> OwnerReplyProposal:
    return OwnerReplyProposal(OwnerReplyIntent.CALENDAR_FOLLOWUP, None, Confidence.HIGH,
                              frozenset({CalendarStatus.CONFIRMED}))


def test_approval_like_replies_the_model_calls_calendar_followups_approve_nothing() -> None:
    chat, request = week_with_one_pending()
    for phrase in ("confirmed please", "yes please", "ok thanks", "confirmed it", "yes it"):
        chat.ask("What is next week looking like?")
        chat.model.script[phrase] = only_confirmed()
        reply = chat.ask(phrase)
        assert not reply.committed, phrase
        assert "Avery Example" in reply.text and "Blake Sample" not in reply.text, phrase
        assert status_of(chat, request) == CalendarStatus.PENDING_APPROVAL, phrase


def test_model_context_is_small_and_has_no_phone_numbers_or_full_names() -> None:
    chat, _request = week_with_one_pending()
    chat.ask("yes please")
    body, context = chat.model.classified[-1]
    assert body == "yes please"
    assert context.last_kind == "calendar_answer" and context.named is None
    assert [(item.client, item.when) for item in context.pending] == [
        ("Blake", "Wed Oct 7 at 9:00 AM")]
    assert "Sample" not in repr(context) and "+1415" not in repr(context)


def test_an_approval_needs_the_request_to_have_been_named_first() -> None:
    chat, request = week_with_one_pending()
    chat.model.script["yes please"] = decision(OwnerReplyIntent.APPROVE_NAMED_REQUEST, request)
    first = chat.ask("yes please")  # Nothing was named yet, so even a confident approve asks.
    assert not first.committed
    assert first.text.endswith("Reply APPROVE or DECLINE to decide it, or ask me about the calendar.")
    assert "Blake Sample, Wed Oct 7 at 9:00 AM" in first.text
    assert status_of(chat, request) == CalendarStatus.PENDING_APPROVAL
    approved = chat.ask("yes please")  # Same words now answer the named request.
    assert approved.committed and approved.text.startswith("Approved: Blake Sample")
    assert chat.model.classified[-1][1].named is not None
    assert status_of(chat, request) == CalendarStatus.CONFIRMED


def test_a_correct_decline_after_the_request_was_named_declines_it() -> None:
    chat, request = week_with_one_pending()
    chat.ask("ok")  # Unclear: asks, naming the request.
    chat.model.script["no thanks, decline it"] = decision(
        OwnerReplyIntent.DECLINE_NAMED_REQUEST, request)
    declined = chat.ask("no thanks, decline it")
    assert declined.committed and declined.text.startswith("Declined: Blake Sample")
    assert status_of(chat, request) == CalendarStatus.DECLINED


def test_wrong_missing_or_short_references_never_approve() -> None:
    chat, request = week_with_one_pending()
    chat.ask("ok")  # Names the request.
    before = chat.snapshot()
    for reference in ("deadbeef", None, request[:4], "00000000"):
        chat.model.script["yes please"] = OwnerReplyProposal(
            OwnerReplyIntent.APPROVE_NAMED_REQUEST, reference, Confidence.HIGH)
        reply = chat.ask("yes please")
        assert not reply.committed, reference
        assert reply.text.startswith("That request may have changed."), reference
    assert chat.snapshot() == before


def test_unclear_low_confidence_errors_and_timeouts_ask_and_write_nothing() -> None:
    chat, request = week_with_one_pending()
    chat.ask("ok")
    before = chat.snapshot()
    chat.model.script["yes please"] = decision(
        OwnerReplyIntent.APPROVE_NAMED_REQUEST, request, Confidence.MEDIUM)
    assert "wasn't sure" in chat.ask("yes please").text
    chat.model.script["yes please"] = decision(
        OwnerReplyIntent.APPROVE_NAMED_REQUEST, request, Confidence.LOW)
    assert "wasn't sure" in chat.ask("yes please").text
    chat.model.script["yes please"] = UNCLEAR
    assert "wasn't sure" in chat.ask("yes please").text
    for error in (RuntimeError("Model API HTTP 500"), TimeoutError(), ValueError("bad schema")):
        chat.model.error = error
        reply = chat.ask("yes please")
        assert not reply.committed and "couldn't tell what you meant" in reply.text
        assert reply.text.endswith("or ask me about the calendar.")
    assert chat.snapshot() == before


def test_without_a_classifier_an_approval_like_reply_still_asks_instead_of_approving() -> None:
    chat, request = week_with_one_pending()
    chat.service._owner_classifier = None
    reply = chat.ask("yes please")
    assert not reply.committed and "wasn't sure" in reply.text
    assert status_of(chat, request) == CalendarStatus.PENDING_APPROVAL


def test_request_x_gone_and_y_arrived_is_not_approved_on_the_old_question() -> None:
    chat, request_x = week_with_one_pending()
    chat.ask("ok")  # The assistant asked about X by name.
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", request_x, "owner", ActorRole.OWNER, Action.DECLINE, "gone", 1))
    request_y = chat.hold("c3", local(10, 8, 9), "y")
    before = chat.snapshot()
    for phrase in ("yes please", "ok thanks"):
        chat.model.script[phrase] = decision(OwnerReplyIntent.APPROVE_NAMED_REQUEST, request_x)
        stale = chat.ask(phrase)
        assert not stale.committed and stale.text.startswith("That request may have changed.")
        assert "Casey Testperson" in stale.text
    chat.model.script["yes please"] = decision(OwnerReplyIntent.APPROVE_NAMED_REQUEST, request_y)
    after_question = chat.ask("yes please")  # The question now names Y, so this approves Y.
    assert after_question.committed
    assert status_of(chat, request_y) == CalendarStatus.CONFIRMED
    assert chat.snapshot() != before


def test_exact_commands_and_replies_outside_a_calendar_conversation_skip_the_model() -> None:
    chat = Chat()
    request = chat.hold("c1", local(10, 1, 9), "a")
    approved = chat.ask("Yes")  # No calendar question open: unchanged owner behavior.
    assert approved.committed and status_of(chat, request) == CalendarStatus.CONFIRMED
    other = chat.hold("c2", local(10, 5, 9), "b")
    chat.ask("What is next week looking like?")
    exact = chat.ask(f"APPROVE {other[:8]}")
    assert exact.committed and status_of(chat, other) == CalendarStatus.CONFIRMED
    assert chat.model.classified == []


def test_a_full_calendar_question_with_a_status_word_does_not_go_to_the_model() -> None:
    chat, _request = week_with_one_pending()
    reply = chat.ask("How many confirmed visits do we have next week?")
    assert "1 confirmed visit counted" in reply.text
    assert chat.model.classified == []


def test_model_followup_can_change_the_range_but_only_within_limits() -> None:
    chat, request = week_with_one_pending()
    chat.model.script["ok, and the week after"] = OwnerReplyProposal(
        OwnerReplyIntent.CALENDAR_FOLLOWUP, None, Confidence.HIGH, None,
        date(2026, 10, 12), date(2026, 10, 18))
    moved = chat.ask("ok, and the week after")
    assert moved.text.startswith("Mon Oct 12 to Sun Oct 18, 2026")
    chat.model.script["ok, and forever"] = OwnerReplyProposal(
        OwnerReplyIntent.CALENDAR_FOLLOWUP, None, Confidence.HIGH, None,
        date(2026, 10, 12), date(2027, 10, 18))
    assert "wasn't sure" in chat.ask("ok, and forever").text
    assert status_of(chat, request) == CalendarStatus.PENDING_APPROVAL


def paged_week(chat: Chat, blocks: int = 14) -> list[str]:
    for index in range(blocks):
        chat.block(local(10, 6, 8, 0) + timedelta(minutes=index * 30),
                   local(10, 6, 8, 15) + timedelta(minutes=index * 30), f"b{index}")
    return [chat.ask("What is next week looking like?").text]


def entries_of(*pages: str) -> list[str]:
    return [line for page in pages for line in page.splitlines()
            if ": unavailable block" in line or "(confirmed)" in line]


def test_more_restarts_when_the_calendar_changed_between_pages() -> None:
    chat = Chat()
    first = paged_week(chat)[0]
    shown = entries_of(first)
    # An insert changes the list the offsets refer to; MORE must restart, not skip or repeat.
    chat.block(local(10, 6, 16), local(10, 6, 17), "inserted")
    second = chat.ask("more").text
    assert second.startswith("The calendar changed, so this is the updated list from the start.")
    assert "Showing 1-" in second
    assert len(second) <= 480 + 80
    assert entries_of(second)[0] == shown[0]  # Starts again from the first entry.
    assert any("4:00 PM-5:00 PM" in line for line in second.splitlines()) or "Reply MORE" in second


def test_more_pages_through_every_entry_exactly_once_when_nothing_changed() -> None:
    chat = Chat()
    pages = paged_week(chat)
    while "Reply MORE" in pages[-1]:
        pages.append(chat.ask("more").text)
    entries = entries_of(*pages)
    assert len(entries) == 14 and len(set(entries)) == 14


def test_a_cancelled_item_between_pages_restarts_instead_of_skipping() -> None:
    chat = Chat()
    for index in range(8):
        chat.hold("c1" if index % 2 else "c2", local(10, 6 + index // 2, 8 + index % 2 * 4),
                  f"h{index}", confirm=True)
    first = chat.ask("What is next week looking like?").text
    assert "Reply MORE" in first
    shown = entries_of(first)
    cancelled = shown[0]
    ref = next(event.event_id for event in chat.store.read_calendar("pilot").events
               if event.start_at == local(10, 6, 8))
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", ref, "owner", ActorRole.OWNER, Action.CANCEL, "cancel-1", 2))
    second = chat.ask("more").text
    assert second.startswith("The calendar changed")
    assert cancelled not in second  # Never lists the cancelled visit.
    remaining = entries_of(second)
    while "Reply MORE" in second:
        second = chat.ask("more").text
        remaining += entries_of(second)
    assert len(remaining) == 7  # All seven current visits, none skipped.


def test_a_redelivered_more_repeats_its_page_instead_of_skipping_one() -> None:
    chat = Chat()
    first = paged_week(chat)[0]
    page_two = chat.ask("more", "SM-more")
    again = chat.ask("more", "SM-more")  # SQS redelivery of the same message.
    assert again.text == page_two.text
    assert entries_of(again.text)[0] not in entries_of(first)
    third = chat.ask("more")
    assert entries_of(third.text)[0] not in entries_of(first, page_two.text)


def test_week_of_the_fall_back_change_keeps_repeated_hour_entries_distinct() -> None:
    chat = Chat()
    chat.now = datetime(2026, 10, 27, 17, tzinfo=UTC)
    # Sun Nov 1, 2026: clocks go back at 2:00 AM, so 1:15 AM happens twice.
    chat.block(datetime(2026, 11, 1, 8, 15, tzinfo=UTC), datetime(2026, 11, 1, 8, 30, tzinfo=UTC), "pdt")
    chat.block(datetime(2026, 11, 1, 9, 15, tzinfo=UTC), datetime(2026, 11, 1, 9, 30, tzinfo=UTC), "pst")
    text = chat.ask("What is this week looking like?").text
    assert text.splitlines()[0].startswith("Mon Oct 26 to Sun Nov 1, 2026")
    assert "2 unavailable blocks" in text.splitlines()[0]
    assert "Sun Nov 1, 1:15 AM PDT-1:30 AM PDT: unavailable block" in text
    assert "Sun Nov 1, 1:15 AM PST-1:30 AM PST: unavailable block" in text
    assert "Sat Oct 31: nothing scheduled" in text


def test_the_25_hour_fall_back_sunday_counts_one_overnight_block_once() -> None:
    chat = Chat()
    chat.now = datetime(2026, 10, 27, 17, tzinfo=UTC)
    # Midnight PDT Sunday to midnight PST Monday is 25 hours.
    chat.block(datetime(2026, 11, 1, 7, tzinfo=UTC), datetime(2026, 11, 2, 8, tzinfo=UTC), "long")
    sunday = chat.ask("What do I have on 2026-11-01?").text
    assert "Sun Nov 1, from 12:00 AM PDT to Mon Nov 2 12:00 AM: unavailable block" in sunday
    week = chat.ask("What is this week looking like?").text
    assert "1 unavailable block." in week.splitlines()[0]
    assert "Mon Nov 2" not in week.splitlines()[0]
