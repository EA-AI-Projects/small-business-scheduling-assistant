"""Synthetic scheduled selection across settings, client, calendar, and consent actions."""

from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.domain.booking_invitations import (
    InvitationPromoter,
    InvitationSelector,
    scheduled_window,
)
from scheduling.domain.booking_outreach import OutreachRecord, OutreachSettings, update_outreach
from scheduling.domain.client_records import ClientRecordService, HomeSize, RecordConflict
from scheduling.domain.conversation import ConversationService
from scheduling.domain.conversation_state import InMemoryConversationStates
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.outbox import PermanentDeliveryFailure
from scheduling.domain.owner_calendar import OwnerAction, OwnerCalendarCommand, OwnerCalendarService
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

BUSINESS = "pilot"
RUN = datetime(2026, 10, 19, 16, tzinfo=UTC)  # Monday 9:00 PDT


class Consent:
    def __init__(self) -> None:
        self.evidence: dict[str, ConsentEvidence] = {}
        self.stopped: set[str] = set()
        self.outbound: list[str] = []

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return self.evidence.get(f"{business_id}:{phone_e164}")

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return f"{business_id}:{phone_e164}" in self.stopped

    def record_outbound(self, business_id: str, phone_e164: str,
                        provider_id: str, sent_at: datetime) -> None:
        self.outbound.append(provider_id)


class SafeRecords(InMemoryCalendarRepository):
    def __init__(self) -> None:
        super().__init__()
        self.send_claimed = False

    def acquire_client_send(self, business_id: str, client_id: str) -> str:
        assert not self.send_claimed
        self.send_claimed = True
        return "synthetic-lease"

    def release_client_send(self, business_id: str, client_id: str, token: str) -> None:
        assert token == "synthetic-lease"
        self.send_claimed = False


def setup() -> tuple[InMemoryCalendarRepository, Consent, InvitationSelector]:
    records = SafeRecords()
    OwnerPolicyService(records, lambda: RUN).seed(BUSINESS, "owner", "seed")
    consent = Consent()
    selector = InvitationSelector(records, consent)
    return records, consent, selector


def client(records: InMemoryCalendarRepository, consent: Consent, number: int,
           *, active: bool = True, verified: bool = True,
           consented: bool = True) -> str:
    client_id = f"synthetic-{number}"
    phone = f"+14155550{number:03d}"
    service = ClientRecordService(records)
    service.save_profile(BUSINESS, client_id, "Synthetic Client", phone,
                         "123 Test Street", HomeSize.SMALL, 60, active, 0, 180, RUN)
    if verified:
        service.verify_phone(BUSINESS, client_id, phone, RUN)
    if consented:
        consent.evidence[f"{BUSINESS}:{phone}"] = ConsentEvidence(
            BUSINESS, client_id, "Synthetic Client", phone, RUN, "synthetic-v1")
    return client_id


def enable(records: InMemoryCalendarRepository, weeks: int = 1) -> None:
    update_outreach(records, BUSINESS, "owner", f"enable-{weeks}",
                    records.read_outreach(BUSINESS).version,
                    OutreachSettings(True, 0, time(9), weeks))


def appointment(records: InMemoryCalendarRepository, client_id: str,
                start: datetime, key: str) -> str:
    result = OwnerCalendarService(records, lambda: RUN).apply(OwnerCalendarCommand(
        BUSINESS, "owner", key, OwnerAction.CREATE_APPOINTMENT,
        records.read_revision(BUSINESS), client_id=client_id,
        start_at=start, duration_minutes=60))
    assert result.appointment is not None
    return result.appointment.appointment_id


def test_selection_uses_current_calendar_consent_and_durable_client_guard() -> None:
    records, consent, selector = setup()
    eligible = client(records, consent, 101)
    boundary = client(records, consent, 102)
    pending = client(records, consent, 103)
    outside = client(records, consent, 104)
    client(records, consent, 105, active=False)
    client(records, consent, 106, verified=False)
    client(records, consent, 107, consented=False)
    client(records, consent, 108)
    consent.stopped.add("pilot:+14155550108")
    appointment(records, boundary, RUN + timedelta(days=7), "boundary")
    appointment(records, outside, RUN + timedelta(days=8), "outside")
    HoldService(records).create(CreateHold(
        BUSINESS, pending, pending, "pending", RUN + timedelta(days=2), 60), RUN)

    assert selector.run(BUSINESS, RUN).reasons == {"disabled": 1}
    enable(records)
    report = selector.run(BUSINESS, RUN)
    assert report.scheduled and report.examined == 8 and report.queued == 3
    assert report.reasons == {"confirmed": 1, "inactive": 1,
        "unverified": 1, "consent": 1, "opted_out": 1}
    assert {intent.client_id for intent in records._invitation_intents.values()} == {
        eligible, pending, outside}
    assert selector.run(BUSINESS, RUN).queued == 0
    assert selector.run(BUSINESS, RUN + timedelta(minutes=1)).scheduled is False
    # Changing the setting does not clear a queued per-client guard.
    enable(records, 2)
    assert selector.run(BUSINESS, RUN).queued == 0


def test_appointment_cancellation_and_phone_change_affect_next_selection() -> None:
    records, consent, selector = setup()
    target = client(records, consent, 201)
    visit_id = appointment(records, target, RUN + timedelta(days=7), "visit")
    enable(records)
    assert selector.run(BUSINESS, RUN).reasons == {"confirmed": 1}
    LifecycleService(records, lambda: RUN).apply(AppointmentCommand(
        BUSINESS, visit_id, "owner", ActorRole.OWNER, Action.CANCEL, "cancel", 1))
    assert selector.run(BUSINESS, RUN).queued == 1
    intent = next(iter(records._invitation_intents.values()))
    # A profile phone change clears verification; prior consent cannot redirect an intent.
    profile = records.read_profile(BUSINESS, target)
    assert profile is not None
    ClientRecordService(records).save_profile(
        BUSINESS, target, profile.name, "+14155550299", profile.service_address,
        profile.home_size, 60, True, profile.version, 180, RUN)
    assert selector.run(BUSINESS, RUN).reasons == {"unverified": 1}
    assert intent.phone_hash != sha256(b"+14155550299").hexdigest()


def test_opt_out_between_selection_and_reservation_does_not_queue() -> None:
    class ChangingConsent(Consent):
        checks = 0

        def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
            self.checks += 1
            return self.checks > 1

    records, _, _ = setup()
    consent = ChangingConsent()
    client(records, consent, 210)
    enable(records)
    report = InvitationSelector(records, consent).run(BUSINESS, RUN)
    assert report.queued == 0
    assert report.reasons == {"opted_out": 1}
    assert records._invitation_intents == {}


def test_sent_interval_and_suppression_survive_repeated_runs() -> None:
    records, consent, selector = setup()
    client(records, consent, 401)
    enable(records, 2)
    assert selector.run(BUSINESS, RUN).queued == 1
    first = next(iter(records._invitation_intents.values()))
    assert records.promote_invitation(first, records.read_outreach(BUSINESS).version, RUN)
    assert records.claim_invitation_handoff(first)
    assert records.complete_invitation(first, RUN, "synthetic-provider-1")
    assert not records.complete_invitation(first, RUN, "synthetic-provider-1")
    assert selector.run(BUSINESS, RUN).queued == 0
    next_week = RUN + timedelta(days=7)
    assert selector.run(BUSINESS, next_week).queued == 0
    two_weeks = datetime(2026, 11, 2, 17, tzinfo=UTC)  # 9:00 PST after fallback
    assert selector.run(BUSINESS, two_weeks).queued == 1
    second = next(intent for intent in records._invitation_intents.values()
                  if intent.intent_id != first.intent_id)
    assert records.complete_invitation(second, None)
    assert records._invitation_states[second.intent_id] == "SUPPRESSED"
    assert selector.run(BUSINESS, two_weeks).queued == 0
    # A suppressed intent did not move successful-handoff history.
    assert records._invitation_last_sent[(BUSINESS, first.client_id)] == RUN


def test_schedule_uses_local_wall_time_and_first_dst_fold_only() -> None:
    zone = ZoneInfo("America/Los_Angeles")
    first = OutreachRecord(OutreachSettings(True, 6, time(1, 30), 1), 1)
    assert scheduled_window(first, zone, datetime(2026, 11, 1, 8, 30, tzinfo=UTC)) is not None
    assert scheduled_window(first, zone, datetime(2026, 11, 1, 9, 30, tzinfo=UTC)) is None
    missing = OutreachRecord(OutreachSettings(True, 6, time(2, 30), 1), 1)
    assert scheduled_window(missing, zone, datetime(2027, 3, 14, 10, 30, tzinfo=UTC)) is None
    later = OutreachRecord(OutreachSettings(True, 6, time(2, 30), 2), 1)
    window = scheduled_window(later, zone, datetime(2027, 3, 7, 10, 30, tzinfo=UTC))
    assert window is not None
    assert window[1].astimezone(zone).hour == 2


def test_durable_intent_transaction_is_separate_from_sms_outbox() -> None:
    class ConditionalConflict(Exception):
        def __init__(self) -> None:
            self.response = {"Error": {"Code": "TransactionCanceledException"},
                             "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}

    class Client:
        writes: list[dict[str, object]] | None = None

        def transact_write_items(self, **kwargs: object) -> dict[str, object]:
            if self.writes is not None:
                raise ConditionalConflict()
            self.writes = kwargs["TransactItems"]  # type: ignore[assignment]
            return {}

    records, consent, selector = setup()
    client_id = client(records, consent, 301)
    enable(records)
    assert selector.run(BUSINESS, RUN).queued == 1
    intent = next(iter(records._invitation_intents.values()))
    profile = records.read_profile(BUSINESS, client_id)
    assert profile is not None
    fake = Client()
    durable = DynamoDBCalendarRepository(fake, "synthetic-table")  # type: ignore[arg-type]
    assert durable.reserve_invitation(intent, 1, 1, profile)
    assert not durable.reserve_invitation(intent, 1, 1, profile)
    assert fake.writes is not None
    keys = [action.get("Put", action.get("Update", {})).get("Item", {}).get("SK", {}).get("S", "")
            for action in fake.writes]
    assert f"INVITATION#{intent.intent_id}" in keys
    assert not any(key.startswith("OUTBOX#") for key in keys)
    saved = next(action["Put"]["Item"] for action in fake.writes
                 if action.get("Put", {}).get("Item", {}).get("SK", {}).get("S", "")
                 == f"INVITATION#{intent.intent_id}")
    assert "phone_e164" not in saved and "body" not in saved


def test_promoted_outbox_keeps_client_link_for_batched_erasure() -> None:
    class Client:
        writes: list[dict[str, object]] | None = None

        def transact_write_items(self, **kwargs: object) -> dict[str, object]:
            self.writes = kwargs["TransactItems"]  # type: ignore[assignment]
            return {}

    records, consent, selector = setup()
    client_id = client(records, consent, 302)
    enable(records)
    assert selector.run(BUSINESS, RUN).queued == 1
    intent = next(iter(records._invitation_intents.values()))
    fake = Client()
    durable = DynamoDBCalendarRepository(fake, "synthetic-table")  # type: ignore[arg-type]
    assert durable.promote_invitation(intent, 1, RUN)
    assert fake.writes is not None
    outbox = next(action["Put"]["Item"] for action in fake.writes
                  if action.get("Put", {}).get("Item", {}).get("SK", {}).get("S", "")
                  .startswith("OUTBOX#booking-invitation#"))
    assert outbox["client_id"]["S"] == client_id
    assert "phone_e164" not in outbox and "body" not in outbox


def test_invitation_promotes_once_and_sender_uses_exact_copy() -> None:
    records, consent, selector = setup()
    client(records, consent, 501)
    enable(records)
    assert selector.run(BUSINESS, RUN).queued == 1
    promoter = InvitationPromoter(records, consent)
    assert promoter.run(BUSINESS, RUN) == {"promoted": 1}
    assert promoter.run(BUSINESS, RUN) == {}
    outbox = next(iter(records._invitation_outbox.values()))

    class Messages:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        def create(self, **kwargs: str) -> SimpleNamespace:
            self.calls.append(kwargs)
            return SimpleNamespace(sid="SM-synthetic-invitation")

    messages = Messages()
    sender = TwilioSmsSender(messages, records, consent, BUSINESS, "+14155550000",
                             "+14155559999", clock=lambda: RUN,
                             invitation_send_enabled=lambda: True)
    assert sender.deliver(outbox) == "SM-synthetic-invitation"
    assert messages.calls[0]["body"] == (
        "Smart Scheduling Assistant: Would you like to book a cleaning visit in the next "
        "one week? Reply with a day and time that works for you, or STOP to opt out.")
    assert "Synthetic Client" not in messages.calls[0]["body"]
    assert consent.outbound == ["SM-synthetic-invitation"]
    assert records.read_last_invitation_sent(BUSINESS, "synthetic-501") == RUN
    assert sender.deliver(outbox) == "SM-synthetic-invitation"
    assert len(messages.calls) == 1

    class NoModel:
        def propose(self, body: str, context: object) -> None:
            raise AssertionError("Exact command must not invoke the model")

    conversation = ConversationService(
        records, NoModel(), HoldService(records), LifecycleService(records, lambda: RUN),
        consent, lambda: RUN, "+14155559999", InMemoryConversationStates())
    result = conversation.handle(InboundReceipt(
        BUSINESS, "SM-synthetic-reply", "+14155550501", "+14155550000",
        "BOOK 2026-10-20 09:00", RUN, SenderRole.CLIENT, "synthetic-501",
        Keyword.OTHER, True))
    assert result.committed
    assert "owner approval" in result.text.lower()


@pytest.mark.parametrize("change", ["booking", "optout", "settings", "phone", "global"])
def test_invitation_suppresses_changed_state_before_provider_handoff(change: str) -> None:
    records, consent, selector = setup()
    target = client(records, consent, 601)
    enable(records)
    assert selector.run(BUSINESS, RUN).queued == 1
    assert InvitationPromoter(records, consent).run(BUSINESS, RUN) == {"promoted": 1}
    outbox = next(iter(records._invitation_outbox.values()))
    intent = next(iter(records._invitation_intents.values()))
    if change == "booking":
        appointment(records, target, RUN + timedelta(days=1), "new-booking")
    elif change == "optout":
        consent.stopped.add("pilot:+14155550601")
    elif change == "settings":
        update_outreach(records, BUSINESS, "owner", "disable", 1,
                        OutreachSettings(False, 0, time(9), 1))
    elif change == "phone":
        profile = records.read_profile(BUSINESS, target)
        assert profile is not None
        ClientRecordService(records).save_profile(BUSINESS, target, profile.name,
            "+14155550699", profile.service_address, profile.home_size, 60,
            True, profile.version, 180, RUN)

    class Messages:
        calls = 0

        def create(self, **kwargs: str) -> SimpleNamespace:
            self.calls += 1
            return SimpleNamespace(sid="SM-unexpected")

    messages = Messages()
    sender = TwilioSmsSender(messages, records, consent, BUSINESS, "+14155550000",
                             "+14155559999", clock=lambda: RUN,
                             invitation_send_enabled=lambda: change != "global")
    with pytest.raises(PermanentDeliveryFailure):
        sender.deliver(outbox)
    assert messages.calls == 0
    assert records.read_invitation(BUSINESS, intent.intent_id).state == "SUPPRESSED"  # type: ignore[union-attr]
    assert records.read_last_invitation_sent(BUSINESS, target) is None


def test_stop_arriving_after_send_claim_suppresses_without_provider_call() -> None:
    records, consent, selector = setup()
    client(records, consent, 701)
    enable(records)
    assert selector.run(BUSINESS, RUN).queued == 1
    assert InvitationPromoter(records, consent).run(BUSINESS, RUN) == {"promoted": 1}
    outbox = next(iter(records._invitation_outbox.values()))

    original_acquire = records.acquire_client_send

    def acquire(business_id: str, client_id: str) -> str:
        token = original_acquire(business_id, client_id)
        consent.stopped.add("pilot:+14155550701")
        return token

    records.acquire_client_send = acquire  # type: ignore[method-assign]

    class Messages:
        calls = 0

        def create(self, **kwargs: str) -> SimpleNamespace:
            self.calls += 1
            return SimpleNamespace(sid="SM-unexpected")

    messages = Messages()
    sender = TwilioSmsSender(messages, records, consent, BUSINESS, "+14155550000",
                             "+14155559999", clock=lambda: RUN,
                             invitation_send_enabled=lambda: True)
    with pytest.raises(PermanentDeliveryFailure, match="INVITATION_OPTED_OUT"):
        sender.deliver(outbox)
    assert messages.calls == 0
    assert not records.send_claimed


def test_uncertain_provider_acceptance_is_held_for_reconciliation() -> None:
    records, consent, selector = setup()
    target = client(records, consent, 801)
    enable(records)
    assert selector.run(BUSINESS, RUN).queued == 1
    assert InvitationPromoter(records, consent).run(BUSINESS, RUN) == {"promoted": 1}
    outbox = next(iter(records._invitation_outbox.values()))
    intent = next(iter(records._invitation_intents.values()))

    class Messages:
        calls = 0

        def create(self, **kwargs: str) -> None:
            self.calls += 1
            raise RuntimeError("Synthetic provider timeout after possible acceptance")

    messages = Messages()
    sender = TwilioSmsSender(messages, records, consent, BUSINESS, "+14155550000",
                             "+14155559999", clock=lambda: RUN,
                             invitation_send_enabled=lambda: True)
    with pytest.raises(PermanentDeliveryFailure, match="INVITATION_HANDOFF_UNCERTAIN"):
        sender.deliver(outbox)
    with pytest.raises(PermanentDeliveryFailure, match="INVITATION_NOT_SENDABLE"):
        sender.deliver(outbox)
    assert messages.calls == 1
    assert records.read_invitation(BUSINESS, intent.intent_id).state == "SENDING"  # type: ignore[union-attr]
    assert records._invitation_pending[(BUSINESS, target)] == intent.intent_id


def test_busy_client_send_suppresses_without_holding_future_invitations() -> None:
    records, consent, selector = setup()
    target = client(records, consent, 901)
    enable(records)
    assert selector.run(BUSINESS, RUN).queued == 1
    assert InvitationPromoter(records, consent).run(BUSINESS, RUN) == {"promoted": 1}
    outbox = next(iter(records._invitation_outbox.values()))
    intent = next(iter(records._invitation_intents.values()))

    def busy(_business_id: str, _client_id: str) -> str:
        raise RecordConflict("Another client send holds the lock")

    records.acquire_client_send = busy  # type: ignore[method-assign]

    class Messages:
        calls = 0

        def create(self, **kwargs: str) -> SimpleNamespace:
            self.calls += 1
            return SimpleNamespace(sid="SM-unexpected")

    messages = Messages()
    sender = TwilioSmsSender(messages, records, consent, BUSINESS, "+14155550000",
                             "+14155559999", clock=lambda: RUN,
                             invitation_send_enabled=lambda: True)
    with pytest.raises(PermanentDeliveryFailure, match="INVITATION_CLIENT_SEND_BUSY"):
        sender.deliver(outbox)
    assert messages.calls == 0
    assert records.read_invitation(BUSINESS, intent.intent_id).state == "SUPPRESSED"  # type: ignore[union-attr]
    assert (BUSINESS, target) not in records._invitation_pending
    assert selector.run(BUSINESS, RUN + timedelta(days=7)).queued == 1
