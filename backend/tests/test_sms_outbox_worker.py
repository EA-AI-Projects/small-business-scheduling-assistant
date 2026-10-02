"""The SMS queue entry point must authorize delivery before creating provider clients."""

from types import SimpleNamespace
from typing import Any

import pytest

from scheduling.workers import sms_outbox

BASE = "https://api.example.test"
READY = {
    "SMS_SEND_ENABLED": "authorized",
    "SMS_API_BASE_URL": BASE,
    "TWILIO_INBOUND_URL": f"{BASE}/webhooks/sms/inbound",
    "TWILIO_STATUS_URL": f"{BASE}/webhooks/sms/status",
}


@pytest.mark.parametrize("change", [
    {"SMS_SEND_ENABLED": "disabled"},
    {"SMS_API_BASE_URL": ""},
    {"TWILIO_INBOUND_URL": "https://other.example.test/webhooks/sms/inbound"},
    {"TWILIO_STATUS_URL": "https://other.example.test/webhooks/sms/status"},
])
def test_sms_worker_rejects_unauthorized_settings_before_creating_clients(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, str],
) -> None:
    for key, value in (READY | change).items():
        monkeypatch.setenv(key, value)

    def unexpected_client(*_args: object, **_kwargs: object) -> None:
        pytest.fail("An unauthorized worker created a provider or AWS client")

    monkeypatch.setattr(sms_outbox, "Client", unexpected_client)
    monkeypatch.setattr(sms_outbox.boto3, "client", unexpected_client)
    with pytest.raises(RuntimeError):
        sms_outbox.handler({"Records": []}, None)


def test_sms_worker_authorized_settings_reach_queue_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in READY.items():
        monkeypatch.setenv(key, value)
    for key, value in {
        "SCHEDULING_TABLE_NAME": "synthetic-table",
        "TWILIO_ACCOUNT_SID": "synthetic-account",
        "TWILIO_AUTH_TOKEN_PARAM": "/synthetic/token",
        "BUSINESS_ID": "synthetic-business",
        "TWILIO_BUSINESS_NUMBER": "+15005550006",
        "OWNER_NUMBER": "+15005550006",
    }.items():
        monkeypatch.setenv(key, value)

    calls: list[tuple[object, ...]] = []
    messages = object()

    def provider(account: str, token: str) -> SimpleNamespace:
        calls.append(("twilio", account, token))
        return SimpleNamespace(messages=messages)

    def consume(records: list[dict[str, Any]], _consumer: object) -> dict[str, list[dict[str, str]]]:
        calls.append(("consume", records))
        return {"batchItemFailures": []}

    monkeypatch.setattr(sms_outbox.boto3, "client", lambda name: name)
    monkeypatch.setattr(sms_outbox, "load_twilio_auth_token", lambda _ssm, _param: "synthetic-token")
    monkeypatch.setattr(sms_outbox, "Client", provider)
    monkeypatch.setattr(sms_outbox, "DynamoDBCalendarRepository", lambda *_: object())
    monkeypatch.setattr(sms_outbox, "DynamoSmsIngressStore", lambda *_: object())
    monkeypatch.setattr(sms_outbox, "DynamoOutboxStore", lambda *_: object())
    monkeypatch.setattr(sms_outbox, "TwilioSmsSender", lambda *_args, **_kwargs: messages)
    monkeypatch.setattr(sms_outbox, "ConsumeService", lambda *_: object())
    monkeypatch.setattr(sms_outbox, "consume_sqs_batch", consume)

    records: list[dict[str, Any]] = []
    assert sms_outbox.handler({"Records": records}, None) == {"batchItemFailures": []}
    assert calls == [("twilio", "synthetic-account", "synthetic-token"), ("consume", records)]
