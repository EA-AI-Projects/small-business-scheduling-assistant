"""Client SMS reads and edits of reminder settings (#402), at the SMS entry point.

Boundary: the real conversation service and receipt flow on one shared synthetic in-memory
repository, a controlled clock, the owner and client HTTP APIs for saved values, the real
reminder selector, and a fake provider behind the real sender. The model is scripted: it
proves what the backend allows, validates, saves and words deterministically, not how the
real model chooses tools or phrases answers. No live model, SMS, or AWS is used.
"""

from dataclasses import replace
from datetime import UTC, datetime, time
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
from scheduling.identity_links import IdentityLink, LinkRole, LinkState
from scheduling.owner_api import OwnerPrincipal, create_owner_app

ZONE = "America/Los_Angeles"
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
    assert "now off" in reply.text and "2 days before" in reply.text and ZONE in reply.text
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
    world.text("Edge", attempt(set_args(days=7, clock="18:00")))
    assert world.saved() == (True, 7, "18:00:00")
    world.text("Edge", attempt(set_args(days=1, clock="08:00")))
    assert world.saved() == (True, 1, "08:00:00")


def test_bare_yes_or_no_cannot_enroll_or_change_timing() -> None:
    world = World()
    before = world.owner_view()
    for body in ("yes", "Yes", "YES", "no"):
        reply = world.text(body, lambda tools: (
            tools("set_reminder_preference", set_args(True, 2, "09:00")), "Reminders on")[1])
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
    unchanged = world.text("Same again", lambda tools: (
        tools("set_reminder_preference", set_args(False, 5, "16:00")), "x")[1],
        receipt_id="SM-same")
    world.owner_edit(enabled=True)
    again = world.text("Same again", forbidden, receipt_id="SM-same")
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
