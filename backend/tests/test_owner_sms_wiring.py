"""The deployed owner factory mounts the SMS consent and delivery-failure routes."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scheduling import owner_auth
from scheduling.owner_api import OwnerPrincipal

ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_pilot"
NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
STAMP = NOW.isoformat()
PROFILE_ITEM = {
    "business_id": {"S": "pilot"}, "client_id": {"S": "client-1"},
    "name": {"S": "Synthetic Client"}, "phone_e164": {"S": "+14155550101"},
    "service_address": {"S": "123 Test Street"}, "home_size": {"S": "small"},
    "default_duration_minutes": {"N": "60"}, "active": {"BOOL": True},
    "version": {"N": "1"}, "created_at": {"S": STAMP}, "updated_at": {"S": STAMP},
}


class FakeDynamo:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return {"Item": PROFILE_ITEM} if kwargs["Key"]["SK"]["S"] == "CLIENT#client-1" else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]
        if prefix == {"S": "SMS_CONSENT#"}:
            return {}  # no earlier consent for this client
        if prefix == {"S": "OUTBOX#"}:
            return {"Items": [
                {"outbox_id": {"S": "out-2"}, "delivery_state": {"S": "FAILED"},
                 "recipient": {"S": "client"}, "last_failed_at": {"S": STAMP},
                 "last_error_code": {"S": "CONSENT_REQUIRED"}},
                {"outbox_id": {"S": "out-1"}, "delivery_state": {"S": "FAILED"},
                 "recipient": {"S": "client"}, "last_failed_at": {"S": STAMP},
                 "last_error_code": {"S": "PROVIDER_ERROR"}},
                {"outbox_id": {"S": "out-3"}, "delivery_state": {"S": "FAILED"},
                 "recipient": {"S": "owner"}, "last_failed_at": {"S": STAMP},
                 "last_error_code": {"S": "RETIRED_BEFORE_LIVE_SMS"}},
            ]}
        assert prefix == {"S": "SMS_STATUS#"}
        return {"Items": [{
            "delivery_status": {"S": "failed"}, "outbox_id": {"S": "out-1"},
            "provider_id": {"S": "SM1"}, "recipient": {"S": "+14155550101"},
            "observed_at": {"S": STAMP}, "error_code": {"S": "30007"},
        }]}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transactions.append(kwargs)
        return {}


class FakeVerifier:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __call__(self, token: str) -> OwnerPrincipal:
        if token != "good":
            raise ValueError("bad token")
        return OwnerPrincipal("owner-sub", "pilot")


def test_cognito_owner_app_records_consent_and_lists_failures(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner_auth, "CognitoOwnerTokenVerifier", FakeVerifier)
    dynamo = FakeDynamo()
    api = TestClient(owner_auth.create_cognito_owner_app(
        dynamo, "scheduling", ISSUER, "client-1", "owner-sub",  # type: ignore[arg-type]
        "pilot", lambda: NOW))
    auth = {"Authorization": "Bearer good"}
    base = "/v1/owner/businesses/pilot"
    body = {"phone_e164": "+14155550101", "participant_name": "Synthetic Client",
            "script_version": "pilot-v1", "clear_yes": True}
    path = f"{base}/clients/client-1/sms-consent"
    assert api.post(path, json=body).status_code == 401
    assert api.post(path, json={**body, "clear_yes": False}, headers=auth).status_code == 422
    assert not dynamo.transactions
    assert api.post(path, json=body, headers=auth).status_code == 200
    assert len(dynamo.transactions) == 1
    failures = api.get(f"{base}/sms-delivery-failures", headers=auth)
    assert failures.status_code == 200
    assert {failure["outbox_id"]: failure["error_code"] for failure in failures.json()} == {
        "out-1": "30007", "out-2": "CONSENT_REQUIRED",
    }
    assert next(f for f in failures.json() if f["outbox_id"] == "out-2")["recipient"] == "client"
