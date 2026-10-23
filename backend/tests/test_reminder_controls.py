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
        'settings': {'days_before': 1, 'local_time': '10:00:00'}, 'version': 0}
    assert api.get(BASE + '/reminders').status_code == 401
    assert api.get(BASE.replace('pilot', 'other') + '/reminders', headers=headers).status_code == 403
    for days, local_time in [(1, '08:00'), (7, '18:00')]:
        version = api.get(BASE + '/reminders', headers=headers).json()['version']
        result = api.put(BASE + '/reminders', json={'expected_version': version,
            'settings': {'days_before': days, 'local_time': local_time}},
            headers={**headers, 'Idempotency-Key': str(version)})
        assert result.status_code == 200, result.text
    saved = api.get(BASE + '/reminders', headers=headers).json()
    for days, local_time in [(0, '10:00'), (8, '10:00'), (1, '07:59'), (1, '18:01'), (1, '10:00:01')]:
        assert api.put(BASE + '/reminders', json={'expected_version': 2,
            'settings': {'days_before': days, 'local_time': local_time}}, headers=headers).status_code == 422
        assert api.get(BASE + '/reminders', headers=headers).json() == saved
    assert api.put(BASE + '/reminders', json={'expected_version': 0,
        'settings': {'days_before': 1, 'local_time': '10:00'}}, headers=headers).status_code == 409

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
