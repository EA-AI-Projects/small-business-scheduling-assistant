"""One shared reminder workflow across owner, portal, SMS, and a fake provider."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_booking_invitation_selection import BUSINESS, RUN, client, setup
from test_client_tool_loop import LoopScript
from test_outbox import MemoryStore

from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.client_api import add_client_session_route
from scheduling.domain.conversation import ConversationService
from scheduling.domain.holds import HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.outbox import ConsumeOutcome, ConsumeService
from scheduling.domain.owner_calendar import OwnerAction, OwnerCalendarCommand, OwnerCalendarService
from scheduling.domain.reminders import ReminderSelector
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.identity_links import IdentityLink, LinkRole, LinkState
from scheduling.owner_api import OwnerPrincipal, create_owner_app


def test_portal_enrollment_sends_once_then_text_unenrollment_suppresses_next_visit() -> None:
    records, consent, _ = setup()
    target = client(records, consent, 501)
    clock = [RUN]
    link = IdentityLink("synthetic-subject", LinkRole.CLIENT, BUSINESS, target,
                        LinkState.ACTIVE, 1, RUN, "owner", "synthetic")
    app = create_owner_app(records, lambda _: OwnerPrincipal("owner", BUSINESS),
                           lambda: clock[0], sms_store=consent)
    add_client_session_route(app, lambda _: link, records, lambda: clock[0], sms_store=consent)
    api = TestClient(app)
    owner = {"Authorization": "Bearer owner", "Idempotency-Key": "reminder-settings"}
    client_headers = {"Authorization": "Bearer client"}
    owner_path = f"/v1/owner/businesses/{BUSINESS}"
    saved = api.put(owner_path + "/reminders", headers=owner,
                     json={"expected_version": 0,
                           "settings": {"days_before": 1, "local_time": "10:00"}})
    assert saved.status_code == 200, saved.text
    state = api.get("/v1/client/reminder-preference", headers=client_headers).json()
    assert state["enabled"] is False
    enrolled = api.put("/v1/client/reminder-preference", headers=client_headers,
                        json={"expected_version": state["version"], "enabled": True})
    assert enrolled.status_code == 200, enrolled.text
    assert enrolled.json()["blockers"] == []
    assert api.get(owner_path + f"/clients/{target}/reminder-preference",
                    headers=owner).json()["enabled"] is True
    calendar = OwnerCalendarService(records, lambda: clock[0])
    for day in (20, 21):
        calendar.apply(OwnerCalendarCommand(
            BUSINESS, "owner", f"visit-{day}", OwnerAction.CREATE_APPOINTMENT,
            records.read_revision(BUSINESS), client_id=target,
            start_at=datetime(2026, 10, day, 17, tzinfo=UTC), duration_minutes=120))
    clock[0] = datetime(2026, 10, 19, 17, 0, 15, tzinfo=UTC)
    selector = ReminderSelector(records, consent)
    assert selector.run(BUSINESS, clock[0]).queued == 1
    submissions = []

    class Messages:
        def create(self, **kwargs):
            submissions.append(kwargs)
            return SimpleNamespace(sid="SM-cross-path-reminder")

    sender = TwilioSmsSender(Messages(), records, consent, BUSINESS, "+14155550000",
                             "+14155559999", clock=lambda: clock[0],
                             reminder_send_enabled=lambda: True)
    outbox = MemoryStore()
    outbox.record = records.list_reminder_outbox()[0]
    consumer = ConsumeService(outbox, sender, lambda: clock[0])
    assert consumer.consume(BUSINESS, outbox.record.outbox_id) == ConsumeOutcome.SENT
    assert consumer.consume(BUSINESS, outbox.record.outbox_id) == ConsumeOutcome.SKIPPED
    assert len(submissions) == 1
    assert "October 20" in submissions[0]["body"]

    def turn_off(tools):
        result = tools("set_reminder_preference", {"enabled": False})
        assert result["ok"] is True
        return "Reminders are now off."

    conversation = ConversationService(records, LoopScript(turn_off), HoldService(records),
        LifecycleService(records, lambda: clock[0]), consent, lambda: clock[0], "+14155559999")
    profile = records.read_profile(BUSINESS, target)
    reply = conversation.handle(InboundReceipt(
        BUSINESS, "SM-cross-path-off", profile.phone_e164, "+14155550000",
        "Please turn off my reminders", clock[0], SenderRole.CLIENT, target, Keyword.OTHER, True))
    assert "off" in reply.text.lower()
    assert api.get("/v1/client/reminder-preference", headers=client_headers).json()["enabled"] is False
    assert api.get(owner_path + f"/clients/{target}/reminder-preference",
                    headers=owner).json()["enabled"] is False
    clock[0] += timedelta(days=1)
    assert selector.run(BUSINESS, clock[0]).queued == 0
    assert len(submissions) == 1
