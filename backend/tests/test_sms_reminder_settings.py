"""Client SMS reads and edits of reminder settings (#402), at the SMS entry point.

Boundary: the real conversation service and receipt flow on one shared synthetic in-memory
repository, a controlled clock, the owner and client HTTP APIs for saved values, the real
reminder selector, and a fake provider behind the real sender. The model is scripted: it
proves what the backend allows, validates, saves and words deterministically, not how the
real model chooses tools or phrases answers. No live model, SMS, or AWS is used.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from test_booking_invitation_selection import BUSINESS, RUN, client, setup
from test_client_tool_loop import LoopScript, Tools
from test_outbox import MemoryStore

from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.client_api import add_client_session_route
from scheduling.domain.client_records import ClientRecordService
from scheduling.domain.conversation import ConversationOutcome, ConversationService
from scheduling.domain.holds import HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.outbox import ConsumeOutcome, ConsumeService
from scheduling.domain.owner_calendar import OwnerAction, OwnerCalendarCommand, OwnerCalendarService
from scheduling.domain.reminders import ReminderSelector
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor
from scheduling.identity_links import IdentityLink, LinkRole, LinkState
from scheduling.owner_api import OwnerPrincipal, create_owner_app

ZONE = "America/Los_Angeles"
NOT_CHANGED = "Reminders not changed."
OWNER = {"Authorization": "Bearer owner", "Idempotency-Key": "settings"}


def set_args(enabled: bool | None = None, days: int | None = None,
             clock: str | None = None) -> dict[str, Any]:
    return {"enabled": enabled, "days_before": days, "local_time": clock}


class World:
    def __init__(self) -> None:
        self.records, self.consent, _ = setup()
        self.clock = [RUN]
        self.target = client(self.records, self.consent, 501)
        self.other = client(self.records, self.consent, 502)
        self.model = LoopScript()
        self.chat = ConversationService(
            self.records, self.model, HoldService(self.records),
            LifecycleService(self.records, lambda: self.clock[0]), self.consent,
            lambda: self.clock[0], "+14155559999")
        self.links = {name: IdentityLink(f"sub-{name}", LinkRole.CLIENT, BUSINESS, name,
                                         LinkState.ACTIVE, 1, RUN, "owner", "synthetic")
                      for name in (self.target, self.other)}
        app = create_owner_app(self.records, lambda _: OwnerPrincipal("owner", BUSINESS),
                               lambda: self.clock[0], sms_store=self.consent)
        add_client_session_route(app, lambda token: self.links[token], self.records,
                                 lambda: self.clock[0], sms_store=self.consent)
        self.api = TestClient(app)
        self.count = 0
        self.submissions: list[dict[str, Any]] = []
        world = self

        class Messages:
            def create(self, **kwargs: Any) -> Any:
                world.submissions.append(kwargs)
                return SimpleNamespace(sid=f"SM-fake-{len(world.submissions)}")

        self.sender = TwilioSmsSender(
            Messages(), self.records, self.consent, BUSINESS, "+14155550000", "+14155559999",
            clock=lambda: self.clock[0], reminder_send_enabled=lambda: True)
        self.delivered: list[str] = []

    # -- saved values through the public APIs -------------------------------------------

    def owner_view(self, who: str | None = None) -> dict[str, Any]:
        response = self.api.get(
            f"/v1/owner/businesses/{BUSINESS}/clients/{who or self.target}/reminder-preference",
            headers=OWNER)
        assert response.status_code == 200, response.text
        return response.json()

    def client_view(self, who: str | None = None) -> dict[str, Any]:
        response = self.api.get("/v1/client/reminder-preference",
                                headers={"Authorization": f"Bearer {who or self.target}"})
        assert response.status_code == 200, response.text
        return response.json()

    def saved(self, who: str | None = None) -> tuple[bool, int, str]:
        view = self.owner_view(who)
        assert view == self.client_view(who)  # One shared record across both surfaces.
        return view["enabled"], view["days_before"], view["local_time"]

    def owner_edit(self, **body: Any) -> None:
        version = self.owner_view()["version"]
        response = self.api.put(
            f"/v1/owner/businesses/{BUSINESS}/clients/{self.target}/reminder-preference",
            headers=OWNER, json={"expected_version": version, **body})
        assert response.status_code == 200, response.text

    # -- SMS entry point -----------------------------------------------------------------

    def receipt(self, body: str, receipt_id: str | None = None, who: int = 501,
                keyword: Keyword = Keyword.OTHER) -> InboundReceipt:
        self.count += 1
        name = self.target if who == 501 else self.other
        return InboundReceipt(
            BUSINESS, receipt_id or f"SM-text-{self.count}", f"+14155550{who:03d}",
            "+14155550000", body, self.clock[0], SenderRole.CLIENT, name, keyword, True)

    def text(self, body: str, plan: Any, **kwargs: Any) -> ConversationOutcome:
        self.model.plan = plan
        return self.chat.handle(self.receipt(body, **kwargs))

    def visit(self, who: str, day: int, hour: int = 17) -> None:
        OwnerCalendarService(self.records, lambda: self.clock[0]).apply(OwnerCalendarCommand(
            BUSINESS, "owner", f"visit-{who}-{day}", OwnerAction.CREATE_APPOINTMENT,
            self.records.read_revision(BUSINESS), client_id=who,
            start_at=datetime(2026, 10, day, hour, tzinfo=UTC), duration_minutes=120))

    def run_delivery(self, moment: datetime) -> int:
        self.clock[0] = moment
        queued = ReminderSelector(self.records, self.consent).run(BUSINESS, moment).queued
        for record in self.records.list_reminder_outbox():
            if record.outbox_id in self.delivered:
                continue
            self.delivered.append(record.outbox_id)
            store = MemoryStore()
            store.record = record
            assert ConsumeService(store, self.sender, lambda: self.clock[0]).consume(
                BUSINESS, record.outbox_id) == ConsumeOutcome.SENT
            # The consumer worked on a copy; mirror its attempt count into the shared store.
            self.records._reminder_outbox[record.outbox_id] = store.record  # type: ignore[attr-defined]
        return queued


def enroll(world: World) -> None:
    """Enroll through SMS with the default timing, as the receipt-backed path always could."""
    world.text("Please remind me about my visits",
               lambda tools: (tools("set_reminder_preference", set_args(enabled=True)),
                              "ok")[1])


# -- reads ---------------------------------------------------------------------------------

def test_read_explains_off_then_scheduled_without_guaranteeing_delivery() -> None:
    world = World()
    world.visit(world.target, 22)
    seen: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        seen.append(tools("get_reminder_preference", {}))
        return "Reminders are off. If on, we'd text 1 day before at 10:00 AM."

    before = world.owner_view()
    reply = world.text("What are my reminder settings?", plan)
    assert reply.text.startswith("Reminders are off") and world.model.rejected == [None]
    assert seen[0]["enabled"] is False and seen[0]["timezone"] == ZONE
    assert [row["status"] for row in seen[0]["reminders"]] == ["disabled"]
    assert world.owner_view() == before  # A read writes nothing.
    enroll(world)

    def second(tools: Tools) -> str:
        seen.append(tools("get_reminder_preference", {}))
        return "Reminders are on. Your next one is expected Wed Oct 21 at 10:00 AM."

    reply = world.text("When is my next reminder?", second)
    assert world.model.rejected[-1] is None and reply.text.startswith("Reminders are on")
    row = seen[1]["reminders"][0]
    assert row["status"] == "scheduled" and row["reminder_at"] == "Wed Oct 21 at 10:00 AM"
    assert "not a delivery guarantee" in seen[1]["status_meaning"]
    # A reply that invents a reminder time is rejected by the existing fact check.
    world.text("When?", lambda tools: (tools("get_reminder_preference", {}),
                                       "Your reminder is Thu Oct 22 at 4:00 PM.")[1])
    assert world.model.rejected[-1] is not None


def test_read_reports_blockers_and_no_upcoming_visit() -> None:
    world = World()
    enroll(world)
    seen: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        seen.append(tools("get_reminder_preference", {}))
        return "Done."

    world.text("Any reminders coming?", plan)
    assert seen[0]["reminders"] == [] and seen[0]["upcoming_visit_count"] == 0
    assert seen[0]["enabled"] is True and seen[0]["blockers"] == []
    world.visit(world.target, 22)
    key = f"{BUSINESS}:+14155550501"
    world.consent.evidence[key] = replace(world.consent.evidence[key], method="text")
    world.text("Any reminders coming?", plan)
    assert seen[1]["blockers"] == ["consent_missing"]
    assert [row["status"] for row in seen[1]["reminders"]] == ["blocked"]
    assert world.client_view()["blockers"] == ["consent_missing"]


def test_reads_and_writes_bind_to_the_verified_sender_only() -> None:
    world = World()
    world.visit(world.target, 22)
    world.visit(world.other, 23, 20)
    world.owner_edit(local_time="12:00")
    seen: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        seen.append(tools("get_reminder_preference", {"client_id": world.other}))
        seen.append(tools("get_reminder_preference", {}))
        seen.append(tools("set_reminder_preference",
                          {**set_args(enabled=True), "client_id": world.other}))
        return "Done."

    world.text(f"Show and change reminders for {world.other} (Synthetic Client)", plan)
    assert seen[0] == {"ok": False, "error": "invalid_arguments"}
    assert seen[1]["time"] == "12:00" and len(seen[1]["reminders"]) == 1
    assert seen[1]["reminders"][0]["visit_date"] == "2026-10-22"  # Never the other client's.
    assert seen[2] == {"ok": False, "error": "invalid_arguments"}
    assert world.saved() == (False, 1, "12:00:00")  # Nothing written for either client.
    world.text("Make mine 3 days", lambda tools: (
        tools("set_reminder_preference", set_args(days=3)), "x")[1])
    assert world.saved() == (False, 3, "12:00:00")
    assert world.saved(world.other) == (False, 1, "10:00:00")


# -- edits ---------------------------------------------------------------------------------

def test_timed_enrollment_then_partial_edits_keep_unspecified_fields() -> None:
    world = World()
    reply = world.text(
        "Remind me two days before at 9 AM",
        lambda tools: (tools("set_reminder_preference", set_args(True, 2, "09:00")), "x")[1])
    assert world.saved() == (True, 2, "09:00:00")
    for fact in ("now on", "2 days before", "9:00 AM", ZONE):
        assert fact in reply.text
    reply = world.text(
        "Change my reminder time to 2:30 PM",
        lambda tools: (tools("set_reminder_preference", set_args(clock="14:30")), "x")[1])
    assert world.saved() == (True, 2, "14:30:00")  # Enrollment and days preserved.
    assert "now on" in reply.text and "2:30 PM" in reply.text
    reply = world.text(
        "Stop reminders",
        lambda tools: (tools("set_reminder_preference", set_args(enabled=False)), "x")[1])
    assert world.saved() == (False, 2, "14:30:00")  # Off retains timing.
    assert "are off" in reply.text and "other permitted" in reply.text.lower() and "2 days before" in reply.text and ZONE in reply.text
    world.text("Make it 4 days ahead",
               lambda tools: (tools("set_reminder_preference", set_args(days=4)), "x")[1])
    assert world.saved() == (False, 4, "14:30:00")  # A timing edit cannot re-enroll.
    world.text("Turn reminders on again",
               lambda tools: (tools("set_reminder_preference", set_args(enabled=True)), "x")[1])
    assert world.saved() == (True, 4, "14:30:00")


def test_ambiguous_or_invalid_timing_never_replaces_saved_settings() -> None:
    world = World()
    world.owner_edit(enabled=True, days_before=3, local_time="11:00")
    before = world.owner_view()
    results: list[dict[str, Any]] = []

    def clarify(tools: Tools) -> str:
        return "Did you mean morning or evening?"  # A bare 9: the model asks and calls nothing.

    reply = world.text("Make my reminder 9", clarify)
    assert reply.text == "Did you mean morning or evening?" and world.owner_view() == before

    def attempt(args: dict[str, Any]) -> Any:
        def plan(tools: Tools) -> str:
            results.append(tools("set_reminder_preference", args))
            return "x"
        return plan

    # A model that passes a bare or malformed time is refused by the backend without a write.
    for args in (set_args(clock="9"), set_args(clock="9 AM"), set_args(clock="25:00"),
                 set_args(days=True), set_args(), {"enabled": None}):  # type: ignore[arg-type]
        world.text("Change my reminders", attempt(args))
        assert results[-1] == {"ok": False, "error": "invalid_arguments"}
        assert world.owner_view() == before
    # Well-formed but out of range: a fixed explanation, saved settings untouched.
    for args in (set_args(days=8), set_args(days=0), set_args(clock="07:59"),
                 set_args(clock="18:01"), set_args(True, 2, "20:00")):
        reply = world.text("Change my reminders", attempt(args))
        assert results[-1]["error"] == "reminder_timing_out_of_range"
        assert "1 to 7 days" in reply.text and "8:00 AM and 6:00 PM" in reply.text
        assert "did not change" in reply.text
        assert world.owner_view() == before
    # Boundaries are accepted.
    world.text("Reminders 7 days before at 18:00", attempt(set_args(days=7, clock="18:00")))
    assert world.saved() == (True, 7, "18:00:00")
    world.text("Reminders 1 day before at 08:00", attempt(set_args(days=1, clock="08:00")))
    assert world.saved() == (True, 1, "08:00:00")


def test_bare_yes_or_no_cannot_enroll_or_change_timing() -> None:
    world = World()
    before = world.owner_view()
    for body in ("yes", "Yes", "YES", "no"):
        reply = world.text(body, lambda tools: (
            tools("set_reminder_preference", set_args(True, 2, "09:00")), "Reminders on")[1])
        assert "Reminders on" not in reply.text and "AM or PM" in reply.text
        assert world.owner_view() == before
        reply = world.text(body, lambda tools: (
            tools("set_reminder_preference", set_args(enabled=True)), "Reminders on")[1])
        assert "Reminders on" not in reply.text and "on or off" in reply.text
        assert world.owner_view() == before


# -- repeated receipts, conflicts, restrictions ----------------------------------------------

def test_repeated_receipt_replays_the_first_result_and_never_overwrites_a_newer_edit() -> None:
    world = World()
    body = "Remind me 2 days before at 9 AM"
    plan = lambda tools: (tools("set_reminder_preference", set_args(True, 2, "09:00")), "x")[1]
    first = world.text(body, plan, receipt_id="SM-dup")
    world.owner_edit(enabled=False, days_before=5, local_time="16:00")
    loops = len(world.model.loops)

    def forbidden(_: Tools) -> str:
        raise AssertionError("A replayed receipt must not run the model")

    replay = world.text(body, forbidden, receipt_id="SM-dup")
    assert replay.text == first.text and len(world.model.loops) == loops
    assert world.saved() == (False, 5, "16:00:00")
    # An edit that left everything unchanged is remembered too.
    unchanged = world.text("Turn reminders off again", lambda tools: (
        tools("set_reminder_preference", set_args(False, 5, "16:00")), "x")[1],
        receipt_id="SM-same")
    world.owner_edit(enabled=True)
    again = world.text("Turn reminders off again", forbidden, receipt_id="SM-same")
    assert again.text == unchanged.text and world.saved() == (True, 5, "16:00:00")


def test_a_newer_preference_is_never_silently_overwritten() -> None:
    world = World()
    original = world.records.commit_reminder_preference_receipt

    def racing(before: Any, after: Any, receipt_id: str) -> None:
        # The owner saves between the SMS read and its commit.
        ClientRecordService(world.records).set_reminder_preference(
            BUSINESS, world.target, True, before.version, world.clock[0],
            days_before=6, local_time=time(15))
        original(before, after, receipt_id)

    world.records.commit_reminder_preference_receipt = racing  # type: ignore[method-assign]
    try:
        reply = world.text("Remind me 2 days before at 9 AM", lambda tools: (
            tools("set_reminder_preference", set_args(True, 2, "09:00")), "Saved!")[1])
    finally:
        world.records.commit_reminder_preference_receipt = original  # type: ignore[method-assign]
    assert "couldn't save" in reply.text and "Saved!" not in reply.text
    assert world.saved() == (True, 6, "15:00:00")
    retry = world.text("Remind me 2 days before at 9 AM", lambda tools: (
        tools("set_reminder_preference", set_args(True, 2, "09:00")), "x")[1])
    assert "2 days before" in retry.text and world.saved() == (True, 2, "09:00:00")


def test_stop_opt_out_and_ineligible_senders_cannot_read_or_change_timing() -> None:
    for restriction in ("stop", "opted_out", "unauthorized", "other_phone", "unverified"):
        world = World()
        receipt = world.receipt("Remind me 3 days before at noon")
        if restriction == "stop":
            receipt = replace(receipt, keyword=Keyword.STOP)
        elif restriction == "opted_out":
            world.consent.stopped.add(f"{BUSINESS}:+14155550501")
        elif restriction == "unauthorized":
            receipt = replace(receipt, authorized_for_commands=False)
        elif restriction == "other_phone":
            receipt = replace(receipt, sender="+14155550109")
        else:
            profile = world.records.read_profile(BUSINESS, world.target)
            assert profile is not None
            world.records.save_profile(
                replace(profile, version=profile.version + 1, phone_verified_at=None),
                profile.version, profile.phone_e164)
        before = world.owner_view()
        world.model.plan = lambda tools: (
            tools("get_reminder_preference", {}),
            tools("set_reminder_preference", set_args(True, 3, "12:00")), "x")[2]
        world.chat.handle(receipt)
        assert world.model.loops == [], restriction
        assert world.owner_view() == before, restriction


# -- past-time explanations and controlled scheduled delivery ----------------------------------

def test_edit_that_moves_a_reminder_into_the_past_is_explained_and_sends_no_catch_up() -> None:
    world = World()
    world.visit(world.target, 22)  # Thu Oct 22 10:00 PDT.
    enroll(world)
    world.clock[0] = datetime(2026, 10, 21, 19, 0, tzinfo=UTC)  # Wed noon PDT.
    reply = world.text(
        "Change my reminder time to 9 AM",
        lambda tools: (tools("set_reminder_preference", set_args(clock="09:00")), "x")[1])
    assert world.saved() == (True, 1, "09:00:00")
    assert "already past" in reply.text and "Thu Oct 22 at 10:00 AM" in reply.text
    assert "no reminder will be sent" in reply.text
    assert [item["status"] for item in world.client_view()["upcoming_reminders"]] == ["past_time"]
    # No catch-up at the edited minute, the old minute, or later; the provider is untouched.
    for moment in (datetime(2026, 10, 21, 16, 0, 15, tzinfo=UTC),
                   datetime(2026, 10, 21, 17, 0, 15, tzinfo=UTC),
                   datetime(2026, 10, 21, 19, 1, 15, tzinfo=UTC)):
        assert world.run_delivery(moment) == 0
    assert world.submissions == []


def test_edit_after_an_attempted_reminder_does_not_reset_or_resend_it() -> None:
    world = World()
    world.visit(world.target, 22)
    enroll(world)
    assert world.run_delivery(datetime(2026, 10, 21, 17, 0, 15, tzinfo=UTC)) == 1
    assert len(world.submissions) == 1 and "October 22" in world.submissions[0]["body"]
    world.text("Change my reminder time to 11 AM",
               lambda tools: (tools("set_reminder_preference", set_args(clock="11:00")), "x")[1])
    assert [item["status"] for item in world.client_view()["upcoming_reminders"]] == ["attempted"]
    assert world.run_delivery(datetime(2026, 10, 21, 18, 0, 15, tzinfo=UTC)) == 0
    assert len(world.submissions) == 1


def test_scheduled_delivery_honors_the_sms_edit_for_only_that_client() -> None:
    world = World()
    world.visit(world.target, 22)
    world.visit(world.other, 22, 20)
    # The other client keeps the default (portal-enrolled), so only the texting client moves.
    version = world.client_view(world.other)["version"]
    assert world.api.put("/v1/client/reminder-preference",
                         headers={"Authorization": f"Bearer {world.other}"},
                         json={"expected_version": version, "enabled": True}).status_code == 200
    world.text("Remind me two days before at 9 AM", lambda tools: (
        tools("set_reminder_preference", set_args(True, 2, "09:00")), "x")[1])
    assert world.saved() == (True, 2, "09:00:00")
    assert world.run_delivery(datetime(2026, 10, 20, 16, 0, 15, tzinfo=UTC)) == 1
    assert len(world.submissions) == 1
    assert world.submissions[0]["to"] == "+14155550501"
    assert "October 22" in world.submissions[0]["body"]
    assert world.run_delivery(datetime(2026, 10, 20, 16, 0, 45, tzinfo=UTC)) == 0  # Once only.
    assert world.run_delivery(datetime(2026, 10, 21, 17, 0, 15, tzinfo=UTC)) == 1
    assert [item["to"] for item in world.submissions] == ["+14155550501", "+14155550502"]
    # The texting client's old 10:00 minute no longer selects anything.
    assert world.run_delivery(datetime(2026, 10, 20, 17, 0, 15, tzinfo=UTC)) == 0
    assert len(world.submissions) == 2


# -- deterministic guards on the inbound text (review round 1) ---------------------------------

def commit_counter(world: World) -> list[int]:
    calls: list[int] = []
    original = world.records.commit_reminder_preference_receipt

    def counted(before: Any, after: Any, receipt_id: str) -> None:
        calls.append(1)
        original(before, after, receipt_id)

    world.records.commit_reminder_preference_receipt = counted  # type: ignore[method-assign]
    return calls


def test_a_model_that_normalizes_a_bare_hour_cannot_save_it() -> None:
    world = World()
    world.owner_edit(enabled=True, days_before=3, local_time="11:00")
    before = world.owner_view()
    commits = commit_counter(world)
    for body, args in (("Make my reminder 9", set_args(clock="09:00")),
                       ("Remind me at 9", set_args(True, clock="09:00")),
                       ("Remind me 9 o'clock", set_args(True, clock="09:00")),
                       ("Remind me at nine", set_args(True, clock="09:00")),
                       ("Remind me at 9:30", set_args(True, clock="09:30")),
                       ("Remind me 2 days before at 9", set_args(True, 2, "09:00")),
                       ("Remind me two days before at 9", set_args(True, 2, "09:00"))):
        reply = world.text(body, lambda tools, args=args: (
            tools("set_reminder_preference", args), "Saved!")[1])
        assert "AM or PM" in reply.text and "Saved!" not in reply.text, body
        assert world.owner_view() == before, body
    assert commits == []



def test_malformed_call_ends_the_action_so_a_valid_retry_in_the_same_message_cannot_save() -> None:
    world = World()
    before = world.owner_view()
    commits = commit_counter(world)
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        results.append(tools("set_reminder_preference", set_args(clock="9")))  # Malformed.
        # The text clearly says 9 AM, so the ambiguity guard alone would allow this retry.
        results.append(tools("set_reminder_preference", set_args(True, clock="09:00")))
        return "Saved!"

    reply = world.text("Remind me at 9 AM", plan)
    assert [item["error"] for item in results] == ["invalid_arguments", "action_already_attempted"]
    assert "Saved!" not in reply.text and world.owner_view() == before and commits == []


def test_a_clear_hour_saves_and_the_two_step_clarification_completes() -> None:
    world = World()
    for body, args, expected in (
            ("Remind me two days before at 9 AM", set_args(True, 2, "09:00"), (True, 2, "09:00:00")),
            ("Make it 9am", set_args(clock="09:00"), (True, 2, "09:00:00")),
            ("Make it noon", set_args(clock="12:00"), (True, 2, "12:00:00")),
            ("Make it 14:00", set_args(clock="14:00"), (True, 2, "14:00:00")),
            ("Make it 09:30", set_args(clock="09:30"), (True, 2, "09:30:00")),
            ("Make it 9 in the morning", set_args(clock="09:00"), (True, 2, "09:00:00")),
            ("Remind me 3 days before at 5 pm", set_args(days=3, clock="17:00"),
             (True, 3, "17:00:00"))):
        world.text(body, lambda tools, args=args: (
            tools("set_reminder_preference", args), "x")[1])
        assert world.saved() == expected, body
    fresh = World()
    before = fresh.owner_view()
    fresh.text("Make my reminder 9", lambda tools: "Morning or evening?")
    assert fresh.owner_view() == before
    fresh.text("morning", lambda tools: (
        tools("set_reminder_preference", set_args(True, clock="09:00")), "x")[1])
    assert fresh.saved() == (True, 1, "09:00:00")


def test_messages_without_reminder_content_cannot_write() -> None:
    world = World()
    before = world.owner_view()
    commits = commit_counter(world)
    for body in ("go ahead", "do it", "please do", "absolutely", "k", "si", "\U0001F44D"):
        world.text(body, lambda tools: (
            tools("set_reminder_preference", set_args(enabled=True)), "Saved!")[1])
        assert world.owner_view() == before, body
    assert commits == []


def test_malformed_write_gets_a_fixed_reply_not_the_models_success_claim() -> None:
    world = World()
    before = world.owner_view()
    reply = world.text("Change my reminders", lambda tools: (
        tools("set_reminder_preference", set_args(clock="9")), "Your reminder is updated.")[1])
    assert "updated" not in reply.text and "nothing changed" in reply.text
    assert world.owner_view() == before


def test_opt_out_between_model_and_write_is_rechecked() -> None:
    world = World()
    before = world.owner_view()

    def plan(tools: Tools) -> str:
        world.consent.stopped.add(f"{BUSINESS}:+14155550501")  # STOP lands mid-message.
        tools("set_reminder_preference", set_args(True, 2, "09:00"))
        return "Saved!"

    reply = world.text("Remind me two days before at 9 AM", plan)
    assert "couldn't save" in reply.text and world.owner_view()["enabled"] is False
    assert world.owner_view()["version"] == before["version"]


def test_blocked_sender_is_told_reminders_cannot_be_sent_right_now() -> None:
    world = World()
    world.visit(world.target, 22)
    key = f"{BUSINESS}:+14155550501"
    world.consent.evidence[key] = replace(world.consent.evidence[key], method="text")
    reply = world.text("Remind me two days before at 9 AM", lambda tools: (
        tools("set_reminder_preference", set_args(True, 2, "09:00")), "x")[1])
    assert world.saved() == (True, 2, "09:00:00")
    assert "can't be sent to you right now" in reply.text


def test_replay_of_a_marker_without_timing_acknowledges_enrollment_only() -> None:
    from scheduling.domain.client_records import ReminderReceipt

    world = World()
    world.records._reminder_receipts[  # type: ignore[attr-defined]
        (BUSINESS, world.target, "SM-legacy")] = ReminderReceipt(True)
    before = world.owner_view()

    def forbidden(_: Tools) -> str:
        raise AssertionError("A replayed receipt must not run the model")

    reply = world.text("Remind me", forbidden, receipt_id="SM-legacy")
    assert "now on" in reply.text and "days before" not in reply.text
    assert world.owner_view() == before


def test_a_clear_time_the_model_contradicts_or_a_distant_day_part_cannot_save() -> None:
    world = World()
    world.owner_edit(enabled=True, days_before=3, local_time="11:00")
    before = world.owner_view()
    for body, clock in (("Remind me at 9 PM", "09:00"), ("Remind me in the evening", "09:00"),
                        ("Remind me at 9ish", "09:00"), ("Remind me at 900", "09:00"),
                        ("Remind me at 830", "08:30"), ("Remind me at 9 AM", "10:00"),
                        ("I work in the morning, remind me at 9", "09:00"),
                        ("Remind me the night before at 8", "08:00"),
                        ("Remind me tomorrow morning", "13:00")):
        reply = world.text(body, lambda tools, clock=clock: (
            tools("set_reminder_preference", set_args(True, clock=clock)), "Saved!")[1])
        assert "AM or PM" in reply.text and "Saved!" not in reply.text, body
        assert world.owner_view() == before, body


def test_common_spellings_of_a_clear_time_save() -> None:
    world = World()
    for body, clock in (("Remind me at nine AM", "09:00"), ("Remind me at two pm", "14:00"),
                        ("Remind me at 9 a.m.", "09:00"), ("Remind me at 9.30 am", "09:30"),
                        ("Remind me at 12 noon", "12:00"), ("Remind me at 8 at night", "20:00"),
                        ("Remind me morning at 9", "09:00"),
                        ("I want one at 9 AM", "09:00")):
        if clock == "20:00":
            continue  # Outside 08:00-18:00; covered by the range reply.
        world.text(body, lambda tools, clock=clock: (
            tools("set_reminder_preference", set_args(True, clock=clock)), "x")[1])
        assert world.saved()[2] == f"{clock}:00", body
    fresh = World()
    fresh.text("a.m.", lambda tools: (
        tools("set_reminder_preference", set_args(True, clock="09:00")), "x")[1])
    assert fresh.saved() == (True, 1, "09:00:00")


def test_plain_language_requests_in_any_wording_reach_the_model_choice() -> None:
    world = World()
    for body, args, expected in (
            ("Activa mis recordatorios", set_args(True), (True, 1, "10:00:00")),
            ("Apaga los recordatorios", set_args(False), (False, 1, "10:00:00")),
            ("No quiero recordatorios", set_args(False), (False, 1, "10:00:00")),
            ("Alert me about my visits", set_args(True), (True, 1, "10:00:00")),
            ("Give me a heads up about my visits", set_args(True), (True, 1, "10:00:00")),
            ("Don't bug me about visits anymore", set_args(False), (False, 1, "10:00:00")),
            ("dos", set_args(days=2), (False, 2, "10:00:00"))):
        world.text(body, lambda tools, args=args: (
            tools("set_reminder_preference", args), "x")[1])
        assert world.saved() == expected, body


def test_a_malformed_reminder_call_never_replaces_another_actions_reply() -> None:
    from test_client_booking_tool import OFFER_TEXT, accept, offer_plan
    from test_client_cancellation_tool import ASK_TEXT, prepare
    from test_client_tool_loop import chat_with
    from test_conversation_flow import THURSDAY, ask

    from scheduling.domain.calendar import CalendarStatus
    from scheduling.domain.conversation_state import PromptKind

    sender = "+14155550101"
    bad = lambda tools: tools("set_reminder_preference", set_args(clock="9"))

    # request_booking commits; the later malformed reminder call is refused without a trace.
    chat, model = chat_with(offer_plan)
    chat.text("Anything tomorrow?")
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (accept(tools), results.append(bad(tools)),
                                "Requested Wed Sep 30 at 10:00 AM. It is pending owner approval.")[2]
    reply = chat.text("the 10 o'clock one please")
    assert reply.committed and reply.client_outbox_id and reply.appointment_id
    assert reply.text.startswith("Requested Wed Sep 30 at 10:00 AM")
    assert reply.text.endswith(NOT_CHANGED)
    assert chat.status(reply.appointment_id) == CalendarStatus.PENDING_APPROVAL
    assert results == [{"ok": False, "error": "action_already_attempted"}]

    # confirm_cancellation commits.
    chat, model = chat_with(lambda tools: (prepare(tools), ASK_TEXT)[1])
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I can't make Thursday"] = ask(
        "cancel", target_date="2026-10-01")
    chat.text("I can't make Thursday")
    model.plan = lambda tools: (tools("confirm_cancellation", {}), bad(tools),
                                "Cancelled your Thu Oct 1 at 9:00 AM visit.")[2]
    done = chat.text("yes please")
    assert done.committed and done.client_outbox_id
    assert done.text == "Cancelled your Thu Oct 1 at 9:00 AM visit. " + NOT_CHANGED
    assert chat.status(visit) == CalendarStatus.CANCELLED

    # prepare_cancellation stores its question; get_availability stores its offer.
    chat, model = chat_with(lambda tools: (prepare(tools), bad(tools), ASK_TEXT)[2])
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I can't make Thursday"] = ask(
        "cancel", target_date="2026-10-01")
    asked = chat.text("I can't make Thursday")
    assert asked.text == f"{ASK_TEXT} {NOT_CHANGED}" and not asked.committed
    stored = chat.states.read_state("pilot", sender)
    assert stored.kind == PromptKind.CONFIRM_CANCEL and stored.appointment_id == visit
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    chat, model = chat_with(lambda tools: (offer_plan(tools), bad(tools), OFFER_TEXT)[2])
    offered = chat.text("Anything tomorrow?")
    assert offered.text == f"{OFFER_TEXT} {NOT_CHANGED}"
    assert chat.states.read_state("pilot", sender).kind == PromptKind.OFFER

    # Content, bare-yes and ambiguous-hour refusals sit behind the same ordering.
    for body in ("yes", "go ahead", "Remind me at 9"):
        chat, model = chat_with(lambda tools: (offer_plan(tools), tools(
            "set_reminder_preference", set_args(True, clock="09:00")), OFFER_TEXT)[2])
        assert chat.text(body).text == f"{OFFER_TEXT} {NOT_CHANGED}", body
        assert chat.states.read_state("pilot", sender).kind == PromptKind.OFFER, body


def test_reminder_reply_in_a_mixed_message_says_nothing_else_was_acted_on() -> None:
    from test_client_cancellation_tool import ASK_TEXT, prepare
    from test_client_tool_loop import chat_with
    from test_conversation_flow import THURSDAY, ask

    from scheduling.domain.calendar import CalendarStatus
    from scheduling.domain.conversation import REMINDER_SCOPE
    from scheduling.domain.conversation_state import PromptKind

    sender = "+14155550101"
    chat, model = chat_with(lambda tools: (prepare(tools), ASK_TEXT)[1])
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I can't make Thursday"] = ask("cancel", target_date="2026-10-01")
    chat.text("I can't make Thursday")
    model.plan = lambda tools: (tools("set_reminder_preference", set_args(True, clock="09:00")),
                                tools("confirm_cancellation", {}), "Cancelled and set.")[2]
    reply = chat.text("yes cancel it and remind me at 9 AM")
    assert REMINDER_SCOPE in reply.text and "now on" in reply.text and not reply.committed
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    assert chat.states.read_state("pilot", sender).kind == PromptKind.CONFIRM_CANCEL
    assert chat.store.read_profile("pilot", "client-1").reminders_enabled is True
    # A refused reminder call carries the same sentence.
    model.plan = lambda tools: (tools("set_reminder_preference", set_args(clock="9")), "x")[1]
    assert REMINDER_SCOPE in chat.text("yes cancel it and remind me at 9").text
    # A later plain yes still confirms the open question exactly as before.
    model.plan = lambda tools: (tools("confirm_cancellation", {}),
                                "Cancelled your Thu Oct 1 at 9:00 AM visit.")[1]
    done = chat.text("yes")
    assert done.committed and chat.status(visit) == CalendarStatus.CANCELLED


def test_reminder_request_after_a_booking_is_reported_unchanged_and_the_booking_stands() -> None:
    from test_client_booking_tool import accept, offer_plan
    from test_client_tool_loop import chat_with
    from test_conversation_flow import NOW
    from test_sms_processing import Reader

    from scheduling.domain.calendar import CalendarStatus
    from scheduling.domain.sms_processing import ReceiptProcessor

    chat, model = chat_with(offer_plan)
    chat.text("Anything tomorrow?")
    before = chat.store.read_profile("pilot", "client-1")
    model.plan = lambda tools: (accept(tools), tools(
        "set_reminder_preference", set_args(True, clock="09:00")),
        "Requested Wed Sep 30 at 10:00 AM. Your reminder is set too.")[2]
    inbound = InboundReceipt("pilot", "SM-mixed", "+14155550101", "+14155550000",
                             "the 10 o'clock one please, and remind me at 9 AM", NOW,
                             SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    store = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(store, chat.service, "pilot", lambda: NOW)
    first = processor.process("pilot", inbound.provider_id)
    assert isinstance(first, ConversationOutcome) and first.committed
    assert first.client_outbox_id and first.appointment_id
    assert chat.status(first.appointment_id) == CalendarStatus.PENDING_APPROVAL
    assert first.text.endswith(NOT_CHANGED) and "reminder is set" in first.text
    assert chat.store.read_profile("pilot", "client-1") == before  # Nothing saved.
    # The stored reply and committed draft carry the sentence; replay sends nothing new.
    assert [text for _, text in store.replies] == [first.text]
    assert [text for *_, text in store.committed_drafts] == [first.text]
    assert processor.process("pilot", inbound.provider_id) is None
    assert len(chat.calendar()) == 1


# -- committed-reply size, refused attempts, tool limit, refusal scope (final round) ------------

FILLER = (" Thanks so much for your patience, I really appreciate it and I hope everything "
          "goes smoothly for you and your family this week and next week too.")


def limited_reader(inbound: InboundReceipt) -> Any:
    from test_sms_processing import Reader

    from scheduling.domain.client_replies import gsm_septets

    class LimitedReader(Reader):
        """The real adapter drops a committed draft over 160 septets (put_committed_reply)."""
        dropped: list[str]

        def put_committed_reply(self, receipt: Any, outbox_id: str, text: str,
                                token: str) -> bool:
            size = gsm_septets(text)
            if size is None or size > 160:
                self.dropped.append(text)
                return False
            return super().put_committed_reply(receipt, outbox_id, text, token)

    reader = LimitedReader({(BUSINESS, inbound.provider_id): inbound})
    reader.dropped = []
    return reader


def committed_inbound(body: str, provider_id: str) -> InboundReceipt:
    from test_conversation_flow import NOW

    return InboundReceipt("pilot", provider_id, "+14155550101", "+14155550000", body, NOW,
                          SenderRole.CLIENT, "client-1", Keyword.OTHER, True)


def assert_delivered_with_sentence(chat: Any, reader: Any, first: Any, inbound: Any) -> None:
    from scheduling.domain.client_replies import gsm_septets

    assert isinstance(first, ConversationOutcome) and first.committed
    assert first.client_outbox_id and first.appointment_id
    assert reader.dropped == []  # The adapter would have kept it, not fallen back to the notice.
    [(_, outbox_id, draft)] = reader.committed_drafts
    assert outbox_id == first.client_outbox_id and draft == first.text
    assert draft.endswith(NOT_CHANGED) and (gsm_septets(draft) or 999) <= 160
    assert FILLER.strip() not in draft  # Too long for one message: deterministic text used.
    reader.replies.clear()
    assert chat.service is not None
    before = len(reader.committed_drafts)
    assert ReceiptProcessor(reader, chat.service, "pilot", lambda: first_clock()).process(
        "pilot", inbound.provider_id) is None
    assert len(reader.committed_drafts) == before  # Nothing sent twice.


def first_clock() -> datetime:
    from test_conversation_flow import NOW

    return NOW


def test_long_committed_replies_keep_the_not_changed_sentence_within_160_septets() -> None:
    from test_client_booking_tool import accept, offer_plan
    from test_client_cancellation_tool import ASK_TEXT, prepare
    from test_client_reschedule_tool import FRIDAY, move_offered, requested_text
    from test_client_tool_loop import chat_with
    from test_conversation_flow import THURSDAY, ask

    from scheduling.domain.calendar import CalendarStatus

    def run(chat: Any, body: str, provider_id: str) -> tuple[Any, Any, Any]:
        inbound = committed_inbound(body, provider_id)
        reader = limited_reader(inbound)
        processor = ReceiptProcessor(reader, chat.service, "pilot", first_clock)
        return reader, inbound, processor.process("pilot", inbound.provider_id)

    reminder = lambda tools: tools("set_reminder_preference", set_args(True, clock="09:00"))

    chat, model = chat_with(offer_plan)
    chat.text("Anything tomorrow?")
    model.plan = lambda tools: (accept(tools), reminder(tools),
                                "Requested Wed Sep 30 at 10:00 AM. It is pending owner "
                                "approval, not confirmed yet." + FILLER)[2]
    reader, inbound, first = run(chat, "the 10 o'clock one, and remind me at 9 AM", "SM-b1")
    assert_delivered_with_sentence(chat, reader, first, inbound)
    assert chat.status(first.appointment_id) == CalendarStatus.PENDING_APPROVAL

    chat, model, visit, _ = move_offered()
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": visit[:8], "date": FRIDAY, "time": "10:00"})), reminder(tools),
        requested_text(results[0]["ref"]) + FILLER)[2]
    reader, inbound, first = run(chat, "2, and remind me at 9 AM", "SM-b2")
    assert_delivered_with_sentence(chat, reader, first, inbound)
    assert first.text.startswith("Requested a move to")
    assert chat.status(visit) == CalendarStatus.CONFIRMED

    chat, model = chat_with(lambda tools: (prepare(tools), ASK_TEXT)[1])
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I can't make Thursday"] = ask("cancel", target_date="2026-10-01")
    chat.text("I can't make Thursday")
    model.plan = lambda tools: (tools("confirm_cancellation", {}), reminder(tools),
                                "Cancelled your Thu Oct 1 at 9:00 AM visit." + FILLER)[2]
    reader, inbound, first = run(chat, "yes, and remind me at 9 AM", "SM-b3")
    assert_delivered_with_sentence(chat, reader, first, inbound)
    assert chat.status(visit) == CalendarStatus.CANCELLED
    assert chat.store.read_profile("pilot", "client-1").reminders_enabled is False


def test_a_refused_booking_then_a_reminder_write_keeps_the_refusal_and_saves_nothing() -> None:
    from test_client_booking_tool import accept, offer_plan
    from test_client_tool_loop import chat_with

    from scheduling.domain.availability import pilot_policy

    chat, model = chat_with(offer_plan)
    chat.text("Anything tomorrow?")
    chat.store.set_policy_for_test("pilot", pilot_policy({date(2026, 9, 30): ()}))  # The offered day closed: the booking is refused.
    before = chat.store.read_profile("pilot", "client-1")
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(accept(tools)), results.append(tools(
        "set_reminder_preference", set_args(True, clock="09:00"))),
        "That time is no longer open, so nothing was booked.")[2]
    reply = chat.text("the second one please, and remind me at 9 AM")
    assert results[0]["reason"] == "outside_current_hours"
    assert results[1] == {"ok": False, "error": "action_already_attempted"}
    assert reply.text == f"That time is no longer open, so nothing was booked. {NOT_CHANGED}"
    assert not reply.committed and chat.calendar() == ()
    assert chat.states.read_state("pilot", "+14155550101") is None  # As a refused booking does.
    assert chat.store.read_profile("pilot", "client-1") == before


def test_a_reminder_call_past_the_tool_limit_still_says_reminders_were_not_changed() -> None:
    from test_client_tool_loop import chat_with

    chat, model = chat_with(lambda tools: "x")
    before = chat.store.read_profile("pilot", "client-1")
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        for _ in range(3):
            tools("get_business_info", {})
        results.append(tools("set_reminder_preference", set_args(True, clock="09:00")))
        return "We are open on weekdays."

    model.plan = plan
    reply = chat.text("What are your hours? Also remind me at 9 AM")
    assert results == [{"ok": False, "error": "tool_limit"}]
    assert reply.text.endswith(NOT_CHANGED)
    assert chat.store.read_profile("pilot", "client-1") == before


def test_every_backend_reminder_refusal_carries_the_scope_sentence() -> None:
    from scheduling.domain.conversation import REMINDER_SCOPE

    def reply_for(world: World, body: str, args: dict[str, Any]) -> str:
        return world.text(body, lambda tools: (
            tools("set_reminder_preference", args), "model text")[1]).text

    world = World()
    assert REMINDER_SCOPE in reply_for(world, "k", set_args(True))  # Bare acknowledgment.
    assert REMINDER_SCOPE in reply_for(world, "Remind me at 9", set_args(True, clock="09:00"))
    assert REMINDER_SCOPE in reply_for(world, "Remind me 9 days before", set_args(days=9))
    assert REMINDER_SCOPE in reply_for(world, "Change it", {"enabled": "yes"})  # Malformed.
    original = world.records.commit_reminder_preference_receipt

    def fail(*_: Any, error: Exception) -> None:
        raise error

    from scheduling.domain.client_records import RecordConflict
    world.records.commit_reminder_preference_receipt = lambda *a: fail(  # type: ignore[method-assign]
        *a, error=RecordConflict("changed"))
    assert REMINDER_SCOPE in reply_for(world, "Turn reminders on", set_args(True))  # Conflict.
    world.records.commit_reminder_preference_receipt = lambda *a: fail(  # type: ignore[method-assign]
        *a, error=OSError("timeout"))
    assert REMINDER_SCOPE in reply_for(world, "Turn reminders on please", set_args(True))
    world.records.commit_reminder_preference_receipt = original  # type: ignore[method-assign]


def test_a_loop_failure_after_a_refused_reminder_call_is_not_redrafted_without_the_sentence() -> None:
    from test_client_booking_tool import accept, offer_plan
    from test_client_cancellation_tool import ASK_TEXT, prepare
    from test_conversation_flow import THURSDAY, ask

    from scheduling.domain.calendar import CalendarStatus

    reminder = lambda tools: tools("set_reminder_preference", set_args(True, clock="09:00"))

    class Drafting(LoopScript):
        """Offers a valid (fallback) redraft; the fix must never ask for it."""

        def __init__(self, plan: Any = None) -> None:
            super().__init__(plan)
            self.draft_calls: list[str] = []

        def draft_client_reply(self, body: str, context: Any, result: Any) -> str:
            self.draft_calls.append(body)
            return str(result.fallback)

    def chat_with(plan: Any) -> tuple[Any, Drafting]:
        from test_conversation_flow import Harness

        model = Drafting(plan)
        return Harness(model), model

    def timeout(_: Tools) -> str:
        raise TimeoutError("model timed out")

    chat, model = chat_with(offer_plan)
    chat.text("Anything tomorrow?")
    model.plan = lambda tools: (accept(tools), reminder(tools), timeout(tools))[2]
    reply = chat.text("the 10 o'clock one, and remind me at 9 AM")
    assert chat.model.draft_calls == []  # A valid redraft was available but must not run.
    assert reply.committed and reply.client_outbox_id and reply.appointment_id
    assert reply.text.endswith(NOT_CHANGED)
    assert chat.status(reply.appointment_id) == CalendarStatus.PENDING_APPROVAL

    chat, model = chat_with(lambda tools: (prepare(tools), ASK_TEXT)[1])
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I can't make Thursday"] = ask("cancel", target_date="2026-10-01")
    chat.text("I can't make Thursday")
    model.plan = lambda tools: (tools("confirm_cancellation", {}), reminder(tools),
                                timeout(tools))[2]
    done = chat.text("yes, and remind me at 9 AM")
    assert chat.model.draft_calls == []
    assert done.committed and done.client_outbox_id and done.text.endswith(NOT_CHANGED)
    assert chat.status(visit) == CalendarStatus.CANCELLED
