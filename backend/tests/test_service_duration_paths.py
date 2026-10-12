"""One saved service duration drives every booking path; changes reach only future requests (#339)."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_client_requests import BASE, NOW, OWNER, START, auth, request, setup
from test_portal_sms_cross_path import BOOK, both

from scheduling.adapters.dynamodb import encode_policy
from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.availability import InvalidDuration, pilot_policy
from scheduling.domain.business_profile import BusinessProfile
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.domain.service_duration import (
    ServiceDurationChanged,
    ServiceNotConfigured,
    service_duration,
)

OWNER_KEY = {**OWNER, "Idempotency-Key": "owner-key"}


def minutes(repository: InMemoryCalendarRepository, appointment_id: str) -> int:
    found = repository.read_appointment(appointment_id)
    assert found is not None
    return found.duration_minutes


def test_sms_portal_and_owner_requests_all_take_the_one_saved_duration() -> None:
    api, repository, texts = both()
    repository.set_business_profile_for_test("business-1", 90)
    offer = texts.text("b", BOOK)
    assert "90-minute cleaning" in offer.text
    text_hold = texts.text("b", "Yes please")
    assert text_hold.committed and text_hold.appointment_id is not None
    assert minutes(repository, text_hold.appointment_id) == 90
    portal = request(api, "a", "portal-1", START + timedelta(hours=4)).json()  # type: ignore[attr-defined]
    assert portal["duration_minutes"] == 90
    owner = api.post(f"{BASE}/requests", headers=OWNER_KEY, json={
        "client_id": "client-b", "start_at": (START + timedelta(hours=6)).isoformat()})
    assert owner.status_code == 200, owner.text
    assert minutes(repository, owner.json()["hold_id"]) == 90
    availability = api.get("/v1/client/availability?day=2026-09-29", headers=auth("a")).json()
    assert availability["duration_minutes"] == 90


def test_owner_override_wins_for_one_request_without_touching_the_default() -> None:
    api, repository, _ = both()
    owner = api.post(f"{BASE}/requests", headers=OWNER_KEY, json={
        "client_id": "client-a", "start_at": START.isoformat(), "duration_minutes": 150})
    assert owner.status_code == 200, owner.text
    assert minutes(repository, owner.json()["hold_id"]) == 150
    assert repository.read_business_profile("business-1").profile.default_duration_minutes == 60  # type: ignore[union-attr]
    manual = api.post(f"{BASE}/appointments", headers={**OWNER, "Idempotency-Key": "m1"}, json={
        "expected_revision": repository.read_revision("business-1"), "client_id": "client-b",
        "start_at": (START + timedelta(hours=5)).isoformat()})
    assert manual.status_code == 200, manual.text
    assert minutes(repository, manual.json()["appointment"]["appointment_id"]) == 60


def test_changing_the_default_leaves_existing_requests_and_holds_alone() -> None:
    api, repository, _ = both()
    first = request(api, "a", "k1").json()  # type: ignore[attr-defined]
    repository.set_business_profile_for_test("business-1", 120)
    assert minutes(repository, first["appointment_id"]) == 60
    held = api.get("/v1/client/bookings", headers=auth("a")).json()["bookings"]
    assert [item["duration_minutes"] for item in held] == [60]
    later = request(api, "b", "k2", START + timedelta(hours=5)).json()  # type: ignore[attr-defined]
    assert later["duration_minutes"] == 120
    # The earlier hold still blocks exactly its own 60 minutes plus buffer.
    assert minutes(repository, first["appointment_id"]) == 60


def test_stale_text_offer_is_refused_after_the_default_changes() -> None:
    _, repository, texts = both()
    offer = texts.text("a", BOOK)
    assert not offer.committed and "1-hour" in offer.text
    revision = repository.read_revision("business-1")
    repository.set_business_profile_for_test("business-1", 120)
    answer = texts.text("a", "Yes please")
    assert not answer.committed
    assert "visit length changed" in answer.text
    assert repository.read_revision("business-1") == revision
    fresh = texts.text("a", BOOK)
    assert "2-hour" in fresh.text
    assert texts.text("a", "Yes please").committed


def test_stale_portal_quote_is_refused_and_a_fresh_one_books_the_new_length() -> None:
    api, repository, _ = both()
    quoted = api.get("/v1/client/availability?day=2026-09-29",
                     headers=auth("a")).json()["duration_minutes"]
    repository.set_business_profile_for_test("business-1", 120)
    stale = request(api, "a", "k1", quoted_duration_minutes=quoted)
    assert stale.status_code == 409  # type: ignore[attr-defined]
    assert stale.json()["detail"]["code"] == "DURATION_CHANGED"  # type: ignore[attr-defined]
    assert repository.read_revision("business-1") == 0
    fresh = request(api, "a", "k2", quoted_duration_minutes=120)
    assert fresh.status_code == 200  # type: ignore[attr-defined]
    assert fresh.json()["duration_minutes"] == 120  # type: ignore[attr-defined]


def test_unconfigured_profile_blocks_every_booking_path() -> None:
    api, repository, texts = both()
    unconfigured = InMemoryCalendarRepository()
    with pytest.raises(ServiceNotConfigured):
        service_duration(unconfigured, "business-1", pilot_policy())
    repository._profiles.clear()
    assert api.get("/v1/client/availability?day=2026-09-29",
                   headers=auth("a")).status_code == 503
    assert request(api, "a", "k1").status_code == 503  # type: ignore[attr-defined]
    assert "contact the owner" in texts.text("a", BOOK).text
    owner = api.post(f"{BASE}/requests", headers=OWNER_KEY, json={
        "client_id": "client-a", "start_at": START.isoformat()})
    assert owner.status_code == 409
    assert owner.json()["error"]["code"] == "SERVICE_NOT_CONFIGURED"
    assert repository.read_revision("business-1") == 0


def test_default_above_the_current_policy_maximum_is_refused_at_read() -> None:
    _, repository, _ = both()
    policy = pilot_policy()
    repository.set_business_profile_for_test("business-1", policy.maximum_visit_minutes + 30)
    with pytest.raises(InvalidDuration):
        service_duration(repository, "business-1", policy)


def test_write_refuses_when_the_default_changes_between_read_and_commit() -> None:
    class Racing(InMemoryCalendarRepository):
        def read_calendar_for_hold(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.set_business_profile_for_test("business-1", 120)
            return super().read_calendar_for_hold(*args, **kwargs)

    repository = Racing()
    OwnerPolicyService(repository, lambda: NOW).seed("business-1", "owner", "seed")
    repository.set_business_profile_for_test("business-1", 60)
    guarded = CreateHold("business-1", "owner", "client-a", "k", START, 60,
                         service_duration_minutes=60)
    with pytest.raises(ServiceDurationChanged):
        HoldService(repository).create(guarded, NOW)
    assert repository.read_revision("business-1") == 1  # only the policy seed
    # An owner's explicit override is not guarded by the default.
    override = CreateHold("business-1", "owner", "client-a", "k2", START, 60)
    assert HoldService(repository).create(override, NOW).duration_minutes == 60


def test_policy_edit_that_would_invalidate_the_default_is_rejected() -> None:
    api, repository, _ = both()
    OwnerPolicyService(repository, lambda: NOW).seed("business-1", "owner", "seed")
    repository.set_business_profile_for_test("business-1", 240)
    policy = json.loads(encode_policy(pilot_policy()))
    policy["maximum_visit_minutes"] = 180
    revision = repository.read_revision("business-1")
    response = api.put(f"{BASE}/policy", headers={**OWNER, "Idempotency-Key": "lower"}, json={
        "expected_revision": revision, "expected_version": 1, "policy": policy})
    assert response.status_code == 409, response.text
    assert repository.read_revision("business-1") == revision
    repository.set_business_profile_for_test("business-1", 120)
    ok = api.put(f"{BASE}/policy", headers={**OWNER, "Idempotency-Key": "lower2"}, json={
        "expected_revision": revision, "expected_version": 1, "policy": policy})
    assert ok.status_code == 200, ok.text


def test_profile_save_retry_replays_after_the_policy_maximum_is_lowered() -> None:
    api, repository, _ = both()
    OwnerPolicyService(repository, lambda: NOW).seed("business-1", "owner", "seed")
    profile = {"display_name": "Example Cleaning Co", "description": "Home cleaning.",
               "support_contact": "help@example.test", "service_name": "Standard clean",
               "service_description": "Whole-home cleaning visit.",
               "default_duration_minutes": 150}
    url = f"{BASE}/profile"
    headers = {**OWNER, "Idempotency-Key": "profile-1"}
    current = repository.read_business_profile("business-1").version
    saved = api.put(url, headers=headers, json={"expected_version": current, "profile": profile})
    assert saved.status_code == 200, saved.text
    # A policy edit through the API would be refused; lower the maximum directly, as a
    # stale session or an older release could.
    repository.set_policy_for_test("business-1", replace(pilot_policy(), maximum_visit_minutes=120))
    retry = api.put(url, headers=headers, json={"expected_version": current, "profile": profile})
    assert retry.status_code == 200
    assert retry.json() == saved.json()
    # A new, different save above the maximum is still refused.
    over = {**profile, "default_duration_minutes": 121}
    refused = api.put(url, headers={**OWNER, "Idempotency-Key": "profile-2"},
                      json={"expected_version": saved.json()["version"], "profile": over})
    assert refused.status_code == 422


def test_client_profile_no_longer_accepts_home_size_or_duration() -> None:
    api, _, _ = setup()
    body = {"expected_version": 0, "name": "Synthetic Client", "phone_e164": "+15550100009",
            "service_address": "9 Test Street", "active": True}
    url = f"{BASE}/clients/client-new"
    ok = api.put(url, headers=OWNER, json=body)
    assert ok.status_code == 200, ok.text
    assert "home_size" not in ok.json() and "default_duration_minutes" not in ok.json()
    for extra in ({"home_size": "small"}, {"default_duration_minutes": 60}):
        assert api.put(url, headers=OWNER, json={**body, "expected_version": 1,
                                                  **extra}).status_code == 422


def test_business_profile_helper_builds_a_valid_profile() -> None:
    assert isinstance(BusinessProfile("A", "B", "C", "D", "E", 60), BusinessProfile)


def test_legacy_client_item_with_home_size_and_duration_still_reads() -> None:
    from scheduling.adapters.dynamodb import DynamoDBCalendarRepository

    stamp = NOW.isoformat()
    legacy = {
        "business_id": {"S": "business-1"}, "client_id": {"S": "client-1"},
        "name": {"S": "Synthetic Client"}, "phone_e164": {"S": "+15550100001"},
        "service_address": {"S": "1 Test Street"}, "home_size": {"S": "large"},
        "default_duration_minutes": {"N": "180"}, "active": {"BOOL": True},
        "version": {"N": "4"}, "created_at": {"S": stamp}, "updated_at": {"S": stamp},
        "phone_verified_at": {"S": stamp},
    }
    # Identity, version, and verification survive; the retired fields are simply ignored.
    profile = DynamoDBCalendarRepository._profile_from_item(legacy)
    assert (profile.client_id, profile.version, profile.phone_verified_at) == (
        "client-1", 4, NOW)
    assert not hasattr(profile, "home_size") and not hasattr(profile, "default_duration_minutes")
