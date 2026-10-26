"""Controlled-clock reminder workflows, with synthetic provider and queue boundaries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from test_booking_invitation_selection import BUSINESS, RUN, client, setup
from test_outbox import MemoryStore

from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.domain.client_records import ClientRecordService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.outbox import ConsumeOutcome, ConsumeService, DeliveryState
from scheduling.domain.owner_calendar import OwnerAction, OwnerCalendarCommand, OwnerCalendarService
from scheduling.domain.reminders import ReminderSelector, ReminderSettings


def workflow():
    records, consent, _ = setup()
    target = client(records, consent, 501)
    profile = records.read_profile(BUSINESS, target)
    ClientRecordService(records).set_reminder_preference(BUSINESS, target, True, profile.version, RUN)
    visit = OwnerCalendarService(records, lambda: RUN).apply(OwnerCalendarCommand(
        BUSINESS, 'owner', 'book', OwnerAction.CREATE_APPOINTMENT, records.read_revision(BUSINESS),
        start_at=datetime(2026, 10, 20, 17, tzinfo=UTC), duration_minutes=120,
        client_id=target)).appointment
    clock = [datetime(2026, 10, 19, 17, 0, 15, tzinfo=UTC)]
    selector = ReminderSelector(records, consent)
    class Store(MemoryStore):
        @property
        def record(self):
            return records.list_reminder_outbox()[0]
        @record.setter
        def record(self, value):
            if value.template == 'visit_reminder':
                records._reminder_outbox[value.outbox_id] = value
        def mark_suppressed(self, record, now):
            with self.lock:
                assert self.record == record
                self.record = replace(record, state=DeliveryState.PENDING, attempts=0,
                    next_attempt_at=None, dispatch_after=None, lease_token=None)
    store = Store()
    class Messages:
        def __init__(self):
            self.calls = []
            self.failure = None
        def create(self, **kwargs):
            self.calls.append(kwargs)
            if self.failure:
                raise self.failure
            return SimpleNamespace(sid='SM-reminder')
    messages = Messages()
    sender = TwilioSmsSender(messages, records, consent, BUSINESS, '+14155550000',
                             '+14155559999', clock=lambda: clock[0],
                             reminder_send_enabled=lambda: True)
    consumer = ConsumeService(store, sender, lambda: clock[0])
    return SimpleNamespace(records=records, consent=consent, target=target, visit=visit,
        clock=clock, messages=messages, selector=selector, consumer=consumer, store=store,
        sender=sender)


def consume(flow):
    return flow.consumer.consume(BUSINESS, flow.store.record.outbox_id)


def test_due_confirmed_visit_sends_one_standard_current_reminder() -> None:
    flow = workflow()
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 1
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 0
    assert consume(flow) == ConsumeOutcome.SENT
    assert consume(flow) == ConsumeOutcome.SKIPPED
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 0
    body = flow.messages.calls[0]['body']
    saved = flow.records.read_business_profile(BUSINESS).profile
    assert saved.display_name in body and saved.service_name in body
    assert 'October 20, 2026' in body and '10:00 AM PDT (America/Los_Angeles)' in body
    assert 'YES' not in body
    assert len(flow.messages.calls) == 1


def test_unsent_selected_reminder_recalculates_after_future_schedule_edit() -> None:
    flow = workflow()
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 1
    flow.records.save_reminders(BUSINESS, 'owner', 'later', 0, ReminderSettings(1, time(11)), flow.clock[0])
    assert consume(flow) == ConsumeOutcome.SKIPPED
    assert flow.messages.calls == []
    flow.clock[0] = datetime(2026, 10, 19, 18, 0, 15, tzinfo=UTC)
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 1
    assert consume(flow) == ConsumeOutcome.SENT
    assert consume(flow) == ConsumeOutcome.SKIPPED
    assert len(flow.messages.calls) == 1


def test_provider_uncertainty_never_retries_or_reselects_and_keeps_erasure_guard() -> None:
    flow = workflow()
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 1
    flow.messages.failure = TimeoutError('synthetic uncertainty')
    assert consume(flow) == ConsumeOutcome.FAILED
    assert consume(flow) == ConsumeOutcome.SKIPPED
    flow.clock[0] += timedelta(minutes=3)
    assert consume(flow) == ConsumeOutcome.SKIPPED
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 0
    assert len(flow.messages.calls) == 1
    assert flow.records.send_claimed


def test_success_followed_by_completion_write_crash_cannot_resubmit() -> None:
    flow = workflow()
    flow.selector.run(BUSINESS, flow.clock[0])
    def crash(*args):
        raise RuntimeError('completion write failed')
    flow.store.mark_sent = crash
    with pytest.raises(RuntimeError):
        consume(flow)
    flow.clock[0] += timedelta(minutes=3)
    assert consume(flow) == ConsumeOutcome.SKIPPED
    assert len(flow.messages.calls) == 1


@pytest.mark.parametrize('change', ['cancel', 'unenroll', 'stop', 'phone', 'inactive', 'consent', 'expired', 'timezone'])
def test_selected_work_rechecks_current_eligibility_before_provider(change) -> None:
    flow = workflow()
    flow.selector.run(BUSINESS, flow.clock[0])
    profile = flow.records.read_profile(BUSINESS, flow.target)
    clients = ClientRecordService(flow.records)
    if change == 'cancel':
        LifecycleService(flow.records, lambda: flow.clock[0]).apply(AppointmentCommand(
            BUSINESS, flow.visit.appointment_id, 'owner', ActorRole.OWNER, Action.CANCEL, 'cancel', flow.visit.version))
    elif change == 'unenroll':
        clients.set_reminder_preference(BUSINESS, flow.target, False, profile.version, flow.clock[0])
    elif change == 'stop':
        flow.consent.stopped.add(f'{BUSINESS}:{profile.phone_e164}')
    elif change == 'phone':
        clients.save_profile(BUSINESS, flow.target, profile.name, '+14155550999',
            profile.service_address, True, profile.version, flow.clock[0])
    elif change == 'inactive':
        clients.save_profile(BUSINESS, flow.target, profile.name, profile.phone_e164,
            profile.service_address, False, profile.version, flow.clock[0])
    elif change == 'consent':
        flow.consent.evidence.clear()
    elif change == 'expired':
        flow.clock[0] = datetime(2026, 10, 19, 17, 1, tzinfo=UTC)
    else:
        flow.records._policies[BUSINESS] = replace(flow.records.read_policy(BUSINESS), timezone='America/New_York')
    assert consume(flow) == ConsumeOutcome.SKIPPED
    assert flow.messages.calls == []


@pytest.mark.parametrize('late', ['enrollment', 'confirmation', 'settings'])
def test_within_minute_changes_do_not_create_catchup(late) -> None:
    flow = workflow()
    cutoff = datetime(2026, 10, 19, 17, tzinfo=UTC)
    clients = ClientRecordService(flow.records)
    profile = flow.records.read_profile(BUSINESS, flow.target)
    if late == 'enrollment':
        profile = clients.set_reminder_preference(BUSINESS, flow.target, False, profile.version, RUN)
        clients.set_reminder_preference(BUSINESS, flow.target, True, profile.version, cutoff + timedelta(seconds=1))
    elif late == 'confirmation':
        LifecycleService(flow.records, lambda: cutoff + timedelta(seconds=1)).apply(AppointmentCommand(
            BUSINESS, flow.visit.appointment_id, 'owner', ActorRole.OWNER, Action.EDIT, 'edit', flow.visit.version,
            start_at=flow.visit.start_at + timedelta(minutes=30)))
    else:
        flow.records.save_reminders(BUSINESS, 'owner', 'before', 0, ReminderSettings(1, time(11)), RUN)
        flow.records.save_reminders(BUSINESS, 'owner', 'late', 1, ReminderSettings(1, time(10)), cutoff + timedelta(seconds=1))
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 0
    assert flow.messages.calls == []


@pytest.mark.parametrize(('visit_at', 'run_at'), [
    (datetime(2027, 1, 1, 18, tzinfo=UTC), datetime(2026, 12, 31, 18, tzinfo=UTC)),
    (datetime(2026, 11, 1, 18, tzinfo=UTC), datetime(2026, 10, 31, 17, tzinfo=UTC)),
    (datetime(2027, 3, 14, 17, tzinfo=UTC), datetime(2027, 3, 13, 18, tzinfo=UTC)),
])
def test_business_calendar_day_selection_across_year_and_dst(visit_at, run_at) -> None:
    from scheduling.domain.availability import LocalWindow
    flow = workflow()
    policy = flow.records.read_policy(BUSINESS)
    flow.records._policies[BUSINESS] = replace(policy, booking_horizon_days=365,
        holiday_calendar=None, weekly_windows={day: (LocalWindow(time(8), time(18)),) for day in range(7)})
    visit = OwnerCalendarService(flow.records, lambda: RUN).apply(OwnerCalendarCommand(
        BUSINESS, 'owner', 'boundary', OwnerAction.CREATE_APPOINTMENT, flow.records.read_revision(BUSINESS),
        start_at=visit_at, duration_minutes=120, client_id=flow.target)).appointment
    assert flow.selector.run(BUSINESS, run_at).queued == 1
    assert flow.records.list_reminder_outbox()[0].entity_id == visit.appointment_id


def test_concurrent_consumers_submit_at_most_once() -> None:
    flow = workflow()
    flow.selector.run(BUSINESS, flow.clock[0])
    with ThreadPoolExecutor(max_workers=8) as workers:
        outcomes = list(workers.map(lambda _: consume(flow), range(24)))
    assert outcomes.count(ConsumeOutcome.SENT) == 1
    assert len(flow.messages.calls) == 1


def test_ordinary_messages_still_retry_but_reminders_do_not() -> None:
    from test_outbox import Sender
    store = MemoryStore()
    sender = Sender(fail_count=2)
    consumer = ConsumeService(store, sender, lambda: RUN)
    store.record = replace(store.record, next_attempt_at=RUN, dispatch_after=RUN)
    assert consumer.consume(store.record.business_id, store.record.outbox_id) == ConsumeOutcome.RETRY_SCHEDULED
    store.record = replace(store.record, template='visit_reminder', state=DeliveryState.PENDING, attempts=0, next_attempt_at=RUN)
    assert consumer.consume(store.record.business_id, store.record.outbox_id) == ConsumeOutcome.FAILED
    assert consumer.consume(store.record.business_id, store.record.outbox_id) == ConsumeOutcome.SKIPPED


def test_owner_eligibility_explanation_matches_strict_delivery_consent_and_failure() -> None:
    from fastapi.testclient import TestClient

    from scheduling.owner_api import OwnerPrincipal, create_owner_app
    flow = workflow()
    profile = flow.records.read_profile(BUSINESS, flow.target)
    key = f'{BUSINESS}:{profile.phone_e164}'
    evidence = flow.consent.evidence[key]
    flow.consent.evidence[key] = replace(evidence, agreed_at=RUN - timedelta(seconds=1))
    api = TestClient(create_owner_app(flow.records, lambda _: OwnerPrincipal('owner', BUSINESS),
        lambda: flow.clock[0], sms_store=flow.consent))
    path = f'/v1/owner/businesses/{BUSINESS}/clients/{flow.target}/reminder-preference'
    result = api.get(path, headers={'Authorization': 'Bearer synthetic'})
    assert result.status_code == 200
    assert result.json()['blockers'] == ['consent_missing']
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 0
    def unavailable(*args):
        raise RuntimeError('synthetic persistence unavailable')
    flow.consent.read_consent = unavailable
    result = api.get(path, headers={'Authorization': 'Bearer synthetic'})
    assert result.status_code == 200
    assert result.json()['blockers'] == ['consent_unavailable']


def test_reservation_conditions_bind_current_settings_calendar_profile_and_sms_gates() -> None:
    from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
    flow = workflow()
    profile = flow.records.read_profile(BUSINESS, flow.target)
    class Dynamo:
        def transact_write_items(self, **kwargs):
            self.transaction = kwargs['TransactItems']
    dynamo = Dynamo()
    repository = DynamoDBCalendarRepository(dynamo, 'synthetic')
    due = datetime(2026, 10, 19, 17, tzinfo=UTC)
    assert repository.reserve_reminder(flow.visit, profile, 0, 2, due, due)
    checks = [item.get('ConditionCheck', {}) for item in dynamo.transaction]
    keys = [check.get('Key', {}).get('SK', {}).get('S') for check in checks]
    assert 'SETTINGS#VISIT_REMINDERS' in keys and 'CALENDAR#REVISION' in keys
    assert f'CLIENT#{flow.target}' in keys
    assert f'SMS_CONSENT_CURRENT#{profile.phone_e164}' in keys
    assert f'SMS_SUPPRESS#{profile.phone_e164}' in keys
    assert f'SMS_OPTOUT#{profile.phone_e164}' in keys
    put = next(item['Put'] for item in dynamo.transaction if 'Put' in item)
    assert 'attempts = :zero' in put['ConditionExpression']
    assert 'delivery_state = :pending' in put['ConditionExpression']
    assert put['Item']['client_id']['S'] == flow.target


def test_durable_claim_rejects_competing_worker_and_never_recovers_consumed_attempt() -> None:
    from botocore.exceptions import ClientError

    from scheduling.adapters.outbox_aws import DynamoOutboxStore
    from scheduling.domain.outbox import OutboxConflict
    flow = workflow()
    flow.selector.run(BUSINESS, flow.clock[0])
    record = flow.store.record
    class Dynamo:
        def update_item(self, **kwargs):
            self.request = kwargs
            raise ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem')
    dynamo = Dynamo()
    store = DynamoOutboxStore(dynamo, 'synthetic')
    with pytest.raises(OutboxConflict):
        store.claim(record, flow.clock[0], flow.clock[0] + timedelta(minutes=2))
    assert 'delivery_state = :pending' in dynamo.request['ConditionExpression']
    assert 'attribute_not_exists(attempts) OR attempts = :zero' in dynamo.request['ConditionExpression']
    assert 'REMOVE next_attempt_at, dispatch_after, outbox_due_pk, outbox_due_sk' in dynamo.request['UpdateExpression']
    with pytest.raises(OutboxConflict):
        store.claim(replace(record, state=DeliveryState.SENDING, attempts=1), flow.clock[0], flow.clock[0] + timedelta(hours=1))


def test_pending_replacement_preserves_original_and_approved_replacement_can_remind() -> None:
    from scheduling.domain.holds import CreateHold, HoldService
    flow = workflow()
    pending = HoldService(flow.records).create(CreateHold(
        BUSINESS, flow.target, flow.target, 'replace',
        datetime(2026, 10, 21, 17, tzinfo=UTC), 120,
        replaces_appointment_id=flow.visit.appointment_id), RUN)
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 1
    assert flow.records.list_reminder_outbox()[0].entity_id == flow.visit.appointment_id
    assert consume(flow) == ConsumeOutcome.SENT
    LifecycleService(flow.records, lambda: flow.clock[0]).apply(AppointmentCommand(
        BUSINESS, pending.hold_id, 'owner', ActorRole.OWNER, Action.APPROVE, 'approve', 1))
    flow.clock[0] = datetime(2026, 10, 20, 17, 0, 15, tzinfo=UTC)
    assert flow.selector.run(BUSINESS, flow.clock[0]).queued == 1
    replacement = flow.records.list_reminder_outbox()[1]
    assert replacement.entity_id == pending.hold_id
    assert flow.sender.deliver(replacement) == 'SM-reminder'
    assert len(flow.messages.calls) == 2


def test_known_preprovider_failure_appears_through_existing_owner_failure_api() -> None:
    from fastapi.testclient import TestClient

    from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
    from scheduling.domain.sms_ingress import ConsentEvidence
    from scheduling.owner_api import OwnerPrincipal, create_owner_app
    flow = workflow()
    profile = flow.records.read_profile(BUSINESS, flow.target)
    clients = ClientRecordService(flow.records)
    profile = clients.save_profile(BUSINESS, flow.target, profile.name, '+14155550101',
        profile.service_address, True, profile.version, RUN)
    clients.verify_phone(BUSINESS, flow.target, '+14155550101', RUN)
    flow.consent.evidence[f'{BUSINESS}:+14155550101'] = ConsentEvidence(
        BUSINESS, flow.target, profile.name, '+14155550101', RUN, 'synthetic-v1')
    flow.selector.run(BUSINESS, flow.clock[0])
    original = flow.store.mark_failure
    failed = []
    def mark_failure(record, state, due_at, error_code, now):
        original(record, state, due_at, error_code, now)
        failed.append({'outbox_id': {'S': record.outbox_id}, 'delivery_state': {'S': state.value},
                       'recipient': {'S': record.recipient}, 'last_error_code': {'S': error_code},
                       'last_failed_at': {'S': now.isoformat()}})
    flow.store.mark_failure = mark_failure
    assert consume(flow) == ConsumeOutcome.FAILED
    assert flow.messages.calls == []
    class Dynamo:
        def query(self, **kwargs):
            prefix = kwargs['ExpressionAttributeValues'][':prefix']['S']
            return {'Items': failed if prefix == 'OUTBOX#' else []}
    failures = DynamoSmsIngressStore(Dynamo(), 'synthetic')
    app = TestClient(create_owner_app(flow.records,
        lambda token: OwnerPrincipal('owner', BUSINESS), lambda: flow.clock[0], sms_store=failures))
    response = app.get(f'/v1/owner/businesses/{BUSINESS}/sms-delivery-failures',
                       headers={'Authorization': 'Bearer synthetic'})
    assert response.status_code == 200, response.text
    assert response.json()[0]['error_code'] == 'FICTIONAL_NUMBER'
    assert response.json()[0]['provider_id'] is None
