"""Owner API reminders configuration and profile enrollment workflow."""
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.owner_api import OwnerPrincipal, create_owner_app

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
BASE = '/v1/owner/businesses/pilot'


def test_owner_saves_reminder_schedule_and_versioned_profile_preference() -> None:
    repository = InMemoryCalendarRepository()
    api = TestClient(create_owner_app(repository, lambda token: OwnerPrincipal('owner', 'pilot'), lambda: NOW))
    headers = {'Authorization': 'Bearer valid', 'Idempotency-Key': 'schedule'}
    assert api.get(BASE + '/reminders', headers=headers).json() == {
        'settings': {'days_before': 1, 'local_time': '10:00:00'}, 'version': 0, 'updated_at': None}
    assert api.get(BASE + '/reminders').status_code == 401
    assert api.get(BASE.replace('pilot', 'other') + '/reminders', headers=headers).status_code == 403
    assert api.put(BASE + '/reminders', json={'expected_version': 0,
        'settings': {'days_before': 7, 'local_time': '18:00'}}, headers=headers).status_code == 410

    profile_body = {'expected_version': 0, 'name': 'Example Client', 'phone_e164': '+12025550123',
                    'service_address': 'Example address'}
    profile = api.put(BASE + '/clients/example', json=profile_body, headers=headers).json()
    assert profile['reminders_enabled'] is False
    preference_path = BASE + '/clients/example/reminder-preference'
    state = api.get(preference_path, headers=headers).json()
    assert state['enabled'] is False
    assert 'phone_unverified' in state['blockers']
    assert 'consent_unavailable' in state['blockers']
    enrolled = api.put(preference_path, json={'enabled': True, 'expected_version': 1}, headers=headers)
    assert enrolled.status_code == 200, enrolled.text
    assert enrolled.json()['enabled'] is True
    same_choice = api.put(preference_path, json={'enabled': True, 'expected_version': 2}, headers=headers)
    assert same_choice.json()['version'] == 2
    assert api.get(BASE + '/clients/example', headers=headers).json()['reminders_updated_at'] == NOW.isoformat().replace('+00:00', 'Z')
    assert api.put(preference_path, json={'enabled': False, 'expected_version': 1}, headers=headers).status_code == 409
    renamed = api.put(BASE + '/clients/example', json={**profile_body, 'expected_version': 2,
                       'name': 'Renamed Client'}, headers=headers).json()
    assert renamed['reminders_enabled'] is True
    assert renamed['reminders_updated_at'] == NOW.isoformat().replace('+00:00', 'Z')
    assert renamed['phone_verified_at'] is None
    assert api.get(BASE + '/clients/missing/reminder-preference', headers=headers).status_code == 404
    assert api.delete(BASE + '/clients/example', headers=headers).status_code == 204
    assert api.get(preference_path, headers=headers).status_code == 404


def test_owner_partial_timing_saved_values_ranges_and_concurrent_enrollment() -> None:
    from scheduling.domain.client_records import ClientRecordService

    records = InMemoryCalendarRepository()
    service = ClientRecordService(records)
    service.save_profile('pilot', 'example', 'Synthetic Client', '+14155550123',
                         '123 Synthetic Street', True, 0, NOW)
    api = TestClient(create_owner_app(records, lambda _: OwnerPrincipal('owner', 'pilot'), lambda: NOW))
    headers = {'Authorization': 'Bearer synthetic'}
    path = BASE + '/clients/example/reminder-preference'
    initial = api.get(path, headers=headers).json()
    assert (initial['days_before'], initial['local_time'], initial['enabled']) == (1, '10:00:00', False)
    assert initial['timezone'] == 'America/Los_Angeles'
    for days, local_time in [(0, '10:00'), (8, '10:00'), (1.5, '10:00'), (True, '10:00'),
                             (1, '07:59'), (1, '18:01'), (1, '10:00:01'), (1, '10:00:00Z')]:
        result = api.put(path, headers=headers, json={'expected_version': initial['version'],
                         'days_before': days, 'local_time': local_time})
        assert result.status_code == 422, result.text
        assert api.get(path, headers=headers).json() == initial
    for days, local_time in [(1, '08:00'), (7, '18:00'), (2, '09:31')]:
        state = api.get(path, headers=headers).json()
        result = api.put(path, headers=headers, json={'expected_version': state['version'],
                         'days_before': days, 'local_time': local_time})
        assert result.status_code == 200, result.text
        assert result.json()['enabled'] is False
    state = api.get(path, headers=headers).json()
    enrolled = api.put(path, headers=headers, json={'expected_version': state['version'], 'enabled': True})
    assert enrolled.json()['days_before'] == 2 and enrolled.json()['local_time'] == '09:31:00'
    stale = api.put(path, headers=headers, json={'expected_version': state['version'], 'days_before': 5})
    assert stale.status_code == 409
    off = api.put(path, headers=headers, json={'expected_version': enrolled.json()['version'], 'enabled': False})
    assert off.json()['days_before'] == 2 and off.json()['local_time'] == '09:31:00'
    assert records.read_profile('pilot', 'example').phone_verified_at is None
    assert api.put(path.replace('pilot', 'other'), headers=headers,
                   json={'expected_version': off.json()['version'], 'days_before': 3}).status_code == 403


def test_legacy_profiles_keep_enrollment_and_inherit_saved_timing_only_once() -> None:
    from dataclasses import replace
    from datetime import time

    from scheduling.domain.client_records import ClientRecordService
    from scheduling.domain.reminders import ReminderSettings

    records = InMemoryCalendarRepository()
    service = ClientRecordService(records)
    records.save_reminders('pilot', 'owner', 'legacy', 0, ReminderSettings(4, time(9, 37)), NOW)
    for index, enabled in enumerate((False, True)):
        identity = f'legacy-{index}'
        profile = service.save_profile('pilot', identity, 'Synthetic Client', f'+1415555010{index}',
                                       '123 Synthetic Street', True, 0, NOW)
        # Serialized legacy records have neither individualized timing field.
        records._clients[('pilot', identity)] = replace(profile, reminders_enabled=enabled,
            reminder_days_before=None, reminder_local_time=None)
        migrated = records.read_profile('pilot', identity)
        assert migrated.reminders_enabled == enabled
        assert migrated.reminder_days_before == 4 and migrated.reminder_local_time == time(9, 37)
        changed = service.set_reminder_preference('pilot', identity, None, migrated.version,
                                                 NOW, days_before=6)
        assert records.read_profile('pilot', identity) == changed
        assert changed.reminders_enabled == enabled and changed.reminder_days_before == 6
    fresh = service.save_profile('pilot', 'fresh', 'Synthetic New Client', '+14155550103',
                                 '456 Synthetic Street', True, 0, NOW)
    assert fresh.reminder_days_before == 1 and fresh.reminder_local_time == time(10)
    assert fresh.reminders_enabled is False
    absent = service.save_profile('other', 'legacy', 'Synthetic Client', '+14155550104',
                                  '789 Synthetic Street', True, 0, NOW)
    records._clients[('other', 'legacy')] = replace(absent, reminder_days_before=None, reminder_local_time=None)
    defaulted = records.read_profile('other', 'legacy')
    assert defaulted.reminder_days_before == 1 and defaulted.reminder_local_time == time(10)
