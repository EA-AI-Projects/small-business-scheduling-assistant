"""Business-wide scheduling preferences in SMS offers (#382), with a scripted model.

Boundary: the conversation service, the in-memory calendar, preferences and offer memory,
hold and lifecycle services, and a scripted model standing in for the model's tool choices
and text; the receipt processor for replay. It proves which valid starts the backend puts
in a stored offer and that acceptance, revalidation, expiry and replay are unchanged, not
how the real model words an offer. No live model, SMS, or AWS is used.

The scenarios mirror the business-wide portal flags (#380): the same rules, applied by the
same ``recommended_starts``, mark the same valid starts. They are defined here so they merge
easily with a shared scenario module.
"""

import json
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from test_client_tool_loop import LoopScript, Tools, chat_with, second_client
from test_conversation_flow import NOW, THURSDAY, ZONE, Harness, ask
from test_sms_processing import Reader

from scheduling.domain.availability import available_starts, pilot_policy
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.conversation import ConversationOutcome
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.recommendations import recommended_starts
from scheduling.domain.scheduling_preferences import (
    PreferenceRule,
    PreferenceWindow,
    SchedulingPreferences,
    review_token,
)
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

SENDER = "+14155550101"
WED, THU, FRI = "2026-09-30", "2026-10-01", "2026-10-02"
OWNER_TEXT = "Prefer Thursday afternoons for the Example Cleaning crew"
Scenario = tuple[PreferenceRule, ...]


def window(start: str, end: str) -> PreferenceWindow:
    return PreferenceWindow(time.fromisoformat(start), time.fromisoformat(end))


# SMS-local scenarios (#382); #383 will consolidate onto the shared preference_scenarios
# module. Thursday afternoon (13:00-15:00 has eight valid starts), Thursday late (three), and a
# weekday no day in the requested range has.
SMS_THURSDAY_AFTERNOON: Scenario = (PreferenceRule((3,), (window("13:00", "15:00"),)),)
SMS_THURSDAY_LATE: Scenario = (PreferenceRule((3,), (window("14:30", "15:30"),)),)
SMS_SATURDAY_ONLY: Scenario = (PreferenceRule((5,)),)


def activate(chat: Harness, rules: Scenario | None, text: str = OWNER_TEXT) -> None:
    current = chat.store.read_scheduling_preferences("pilot")
    preferences = None if rules is None else SchedulingPreferences(
        text, rules, "America/Los_Angeles", review_token(text, rules, "America/Los_Angeles"))
    chat.store.save_scheduling_preferences(
        "pilot", "owner", f"k{current.version}", current.version, preferences)


def valid_starts(day: str, minutes: int = 120) -> list[datetime]:
    return list(available_starts(pilot_policy(), date.fromisoformat(day), minutes, (), NOW))


def local(options: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(item["date"], item["time"]) for item in options]


def get(tools: Tools, first: str = WED, last: str = FRI, time_from: str | None = None,
        time_to: str | None = None, **extra: Any) -> dict[str, Any]:
    return tools("get_availability", {"from": first, "to": last, "time_from": time_from,
                                      "time_to": time_to, **extra})


def offered(rules: Scenario | None, **kwargs: Any) -> tuple[Harness, LoopScript, dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    chat, model = chat_with(lambda tools: (
        seen.append(get(tools, **kwargs)), ("Here are the open times. The owner approves "
                                                    "every request."))[1])
    if rules is not None:
        activate(chat, rules)
    chat.text("what do you have this week?")
    return chat, model, seen[0]


def stored(chat: Harness) -> list[datetime]:
    state = chat.states.read_state("pilot", SENDER)
    assert state is not None
    return list(state.options)


def portal_flags(day_range: list[str], rules: Scenario) -> set[datetime]:
    """What the client portal would flag for the same valid starts."""
    preferences = SchedulingPreferences(
        OWNER_TEXT, rules, "America/Los_Angeles",
        review_token(OWNER_TEXT, rules, "America/Los_Angeles"))
    starts = [start for day in day_range for start in valid_starts(day)]
    return set(recommended_starts(preferences, "America/Los_Angeles", starts))


def local_day(start: datetime) -> str:
    return start.astimezone(ZONE).date().isoformat()


def test_without_preferences_the_offer_is_the_ordinary_spread() -> None:
    _, _, result = offered(None)
    assert {item["date"] for item in result["offer"]["options"]} == {WED, THU, FRI}
    assert len(result["offer"]["options"]) == 5


def test_preferred_starts_in_the_window_are_offered_first_and_match_the_portal_flags() -> None:
    chat, _, result = offered(SMS_THURSDAY_AFTERNOON)
    options = stored(chat)
    flagged = portal_flags([WED, THU, FRI], SMS_THURSDAY_AFTERNOON)
    assert len(options) == 5 and set(options) <= flagged  # Preferred starts fill the offer.
    assert {local_day(start) for start in options} == {THU}
    assert all(13 <= start.astimezone(ZONE).hour < 15 for start in options)
    assert options == sorted(options)
    assert result["more_open_times_than_offered"] is True


def test_nonpreferred_valid_starts_fill_the_slots_preferred_ones_leave_open() -> None:
    chat, _, _ = offered(SMS_THURSDAY_LATE)
    options = stored(chat)
    flagged = portal_flags([WED, THU, FRI], SMS_THURSDAY_LATE)
    assert len(flagged) == 3 and flagged <= set(options)  # Every preferred start is offered.
    assert len(options) == 5
    every = {start for day in (WED, THU, FRI) for start in valid_starts(day)}
    assert set(options) <= every  # Nothing outside availability is ever offered.
    assert any(start not in flagged for start in options)


def test_an_explicit_window_is_honored_even_when_nothing_in_it_is_preferred() -> None:
    chat, _, result = offered(SMS_THURSDAY_AFTERNOON, time_from="08:00", time_to="10:00")
    options = stored(chat)
    assert result["requested_time_matched"] is True
    assert options and all(8 <= start.astimezone(ZONE).hour <= 10 for start in options)
    assert not set(options) & portal_flags([WED, THU, FRI], SMS_THURSDAY_AFTERNOON)


def test_a_window_with_preferred_starts_orders_them_first_inside_the_window() -> None:
    chat, _, _ = offered(SMS_THURSDAY_AFTERNOON, time_from="12:00", time_to="14:00")
    options = stored(chat)
    flagged = portal_flags([WED, THU, FRI], SMS_THURSDAY_AFTERNOON)
    assert all(12 <= start.astimezone(ZONE).hour <= 14 for start in options)
    preferred = [start for start in options if start in flagged]
    inside = {start for start in flagged if start.astimezone(ZONE).hour <= 14}
    assert preferred and set(preferred) <= inside
    assert {local_day(start) for start in preferred} == {THU}
    assert len(preferred) == 5 or set(preferred) == inside


def test_no_preferred_start_in_range_falls_back_to_the_ordinary_offer() -> None:
    plain_chat, _, plain = offered(None)
    chat, _, result = offered(SMS_SATURDAY_ONLY)
    assert local(result["offer"]["options"]) == local(plain["offer"]["options"])
    assert stored(chat) == stored(plain_chat)


def test_alternatives_still_work_after_a_preferred_offer() -> None:
    chat, model, _ = offered(SMS_THURSDAY_AFTERNOON)
    first = stored(chat)
    model.plan = lambda tools: (get(tools, WED, WED), ("Wednesday is open. "
                                "The owner approves every request."))[1]
    chat.text("what about Wednesday?")
    again = stored(chat)
    assert again != first and {local_day(start) for start in again} == {WED}


def test_a_failing_preference_read_gives_the_ordinary_offer(
        caplog: pytest.LogCaptureFixture) -> None:
    plain_chat, _, _ = offered(None)

    def broken(_business_id: str) -> Any:
        raise RuntimeError(OWNER_TEXT)

    chat, _ = chat_with(lambda tools: (get(tools), ("Open times. "
                                       "The owner approves every request."))[1])
    activate(chat, SMS_THURSDAY_AFTERNOON)
    chat.store.read_scheduling_preferences = broken  # type: ignore[method-assign]
    with caplog.at_level("WARNING"):
        chat.text("what do you have this week?")
    assert stored(chat) == stored(plain_chat)
    assert OWNER_TEXT not in caplog.text and "RuntimeError" in caplog.text


def test_tool_results_expose_no_owner_text_and_mark_nothing() -> None:
    _, _, result = offered(SMS_THURSDAY_AFTERNOON)
    dumped = json.dumps(result).lower()
    assert "afternoons" not in dumped and "crew" not in dumped
    assert "prefer" not in dumped and "recommend" not in dumped


def test_a_preference_change_between_offer_and_acceptance_does_not_invalidate_the_offer() -> None:
    chat, model, _ = offered(SMS_THURSDAY_AFTERNOON)
    options = stored(chat)
    activate(chat, None)  # The owner clears the preference after the offer was sent.
    pick = options[0].astimezone(ZONE)
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_booking", {
        "date": pick.date().isoformat(), "time": pick.strftime("%H:%M")})),
        (f"Requested {pick:%a %b %d} (ref {results[0]['ref']}). It is pending owner approval, "
         "not confirmed yet."))[1]
    booked = chat.text("the first one")
    assert booked.committed and results[0]["requested"] is True
    assert chat.status(booked.appointment_id or "") == CalendarStatus.PENDING_APPROVAL


def test_a_taken_preferred_start_is_revalidated_at_acceptance() -> None:
    chat, model, _ = offered(SMS_THURSDAY_AFTERNOON)
    options = stored(chat)
    second_client(chat)
    HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "taken", options[0], 120), NOW)
    pick = options[0].astimezone(ZONE)
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_booking", {
        "date": pick.date().isoformat(), "time": pick.strftime("%H:%M")})),
        "That time is no longer available. Nothing changed.")[1]
    reply = chat.text("the first one")
    assert not reply.committed and results[0].get("requested") is not True
    assert len(chat.calendar()) == 1  # Only the other client's reservation exists.


def test_an_expired_preferred_offer_cannot_be_accepted() -> None:
    chat, model, _ = offered(SMS_THURSDAY_AFTERNOON)
    chat.now += timedelta(minutes=31)
    loops = len(model.loops)
    late = chat.text("1")
    assert not late.committed and len(model.loops) == loops
    assert chat.calendar() == ()


def test_replay_of_the_offering_receipt_stores_the_same_offer_once() -> None:
    chat, model = chat_with(lambda tools: (get(tools), ("Open times. The owner approves "
                                           "every request."))[1])
    activate(chat, SMS_THURSDAY_AFTERNOON)
    inbound = InboundReceipt("pilot", "SM-pref-1", SENDER, "+14155550000",
                             "what do you have this week?", NOW, SenderRole.CLIENT, "client-1",
                             Keyword.OTHER, True)
    store = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(store, chat.service, "pilot", lambda: NOW)
    first = processor.process("pilot", inbound.provider_id)
    assert isinstance(first, ConversationOutcome)
    options = stored(chat)
    activate(chat, None)  # A change before the replay must not rewrite the stored offer.
    assert processor.process("pilot", inbound.provider_id) is None
    assert stored(chat) == options and len(model.loops) == 1 and len(store.replies) == 1


def test_a_reschedule_offer_prefers_preferred_starts_and_keeps_the_original_version_bound() -> None:
    chat, model = chat_with(lambda tools: (
        get(tools, THU, FRI, ref=tools("get_my_bookings", {
            "from": None, "to": None})["bookings"][0]["ref"]),
        ("To move your visit, times are open. Your Thu Oct 1 at 9:00 AM visit stays "
         "confirmed until the owner approves."))[1])
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    activate(chat, SMS_THURSDAY_AFTERNOON)
    chat.model.replies["move my visit"] = ask("reschedule", target_date=THU)
    chat.text("move my visit")
    options = stored(chat)
    state = chat.states.read_state("pilot", SENDER)
    assert state is not None and state.appointment_id == visit and state.appointment_version == 2
    # The visit being moved does not block its own replacement; preferred starts come first.
    assert set(options) <= portal_flags([THU, FRI], SMS_THURSDAY_AFTERNOON) and len(options) == 5
    pick = options[1].astimezone(ZONE)
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": visit[:8], "date": pick.date().isoformat(), "time": pick.strftime("%H:%M")})),
        (f"Requested a move (ref {results[0]['ref']}). It is pending owner approval. Your Thu "
         "Oct 1 at 9:00 AM visit stays confirmed until the owner approves."))[1]
    activate(chat, None)
    moved = chat.text("2")
    assert moved.committed and results[0]["requested"] is True
    assert chat.status(visit) == CalendarStatus.CONFIRMED  # Until the owner approves.
    assert chat.status(moved.appointment_id or "") == CalendarStatus.PENDING_APPROVAL


def test_the_deterministic_offer_prefers_preferred_starts_too() -> None:
    chat = Harness()
    activate(chat, SMS_THURSDAY_AFTERNOON)
    chat.model.replies["anything this week?"] = ask("availability", WED, FRI)
    offer = chat.text("anything this week?")
    options = stored(chat)
    assert not offer.committed
    assert set(options) <= portal_flags([WED, THU, FRI], SMS_THURSDAY_AFTERNOON)
    assert len(options) == 5 and "afternoons" not in offer.text
    assert chat.calendar() == ()
    activate(chat, None)
    booked = chat.text("1")
    assert booked.committed
    assert chat.status(booked.appointment_id or "") == CalendarStatus.PENDING_APPROVAL


def test_the_deterministic_explicit_window_is_honored_without_a_preferred_start() -> None:
    chat = Harness()
    activate(chat, SMS_THURSDAY_AFTERNOON)
    chat.model.replies["mornings this week"] = ask("availability", WED, FRI, "08:00", "10:00")
    chat.text("mornings this week")
    options = stored(chat)
    assert options and all(8 <= start.astimezone(ZONE).hour <= 10 for start in options)


WED_AND_THU: Scenario = (
    PreferenceRule((2,), (window("15:00", "15:30"),)),  # Only the 15:00 start (120-minute visits, 17:00 close).
    PreferenceRule((3,), (window("13:00", "15:00"),)))


def test_a_day_with_one_preferred_start_does_not_cost_the_other_day_its_preferred_starts() -> None:
    chat, _, _ = offered(WED_AND_THU, first=WED, last=THU)
    options = stored(chat)
    flagged = portal_flags([WED, THU], WED_AND_THU)
    assert len(flagged) > 5
    assert len(options) == 5 and set(options) <= flagged  # No nonpreferred start is offered.
    assert options == sorted(options)


def test_sms_preferred_starts_are_a_subset_of_the_portal_recommendations() -> None:
    from test_scheduling_preferences import activate as portal_activate
    from test_scheduling_preferences import availability, review, setup

    api, _, _ = setup()
    portal_activate(api, review(api, "Prefer Tuesday mornings"), 0)
    recommended = {datetime.fromisoformat(v)
                   for v in availability(api, "2026-09-29")["recommended_starts_at"]}
    assert recommended
    rules = (PreferenceRule((1,), (window("09:00", "12:00"),)),)
    chat, _, _ = offered(rules, first="2026-09-29", last="2026-09-29")
    options = stored(chat)
    # Same rule, calendar and day: every start SMS offers is one the portal recommends.
    assert options and set(options) <= recommended
