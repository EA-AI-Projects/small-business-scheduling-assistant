"""Synthetic scheduled selection across settings, client, calendar, and consent actions."""

from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.booking_invitations import InvitationSelector, scheduled_window
from scheduling.domain.booking_outreach import OutreachRecord, OutreachSettings, update_outreach
from scheduling.domain.client_records import ClientRecordService, HomeSize
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.owner_calendar import OwnerAction, OwnerCalendarCommand, OwnerCalendarService
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.domain.sms_ingress import ConsentEvidence

BUSINESS = "pilot"
RUN = datetime(2026, 10, 19, 16, tzinfo=UTC)  # Monday 9:00 PDT


class Consent:
    def __init__(self) -> None:
        self.evidence: dict[str, ConsentEvidence] = {}
        self.stopped: set[str] = set()

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return self.evidence.get(f"{business_id}:{phone_e164}")

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return f"{business_id}:{phone_e164}" in self.stopped


def setup() -> tuple[InMemoryCalendarRepository, Consent, InvitationSelector]:
    records = InMemoryCalendarRepository()
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
    assert records.complete_invitation(first, RUN)
    assert not records.complete_invitation(first, RUN)
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
