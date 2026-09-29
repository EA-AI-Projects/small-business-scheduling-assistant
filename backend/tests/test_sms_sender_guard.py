"""A sender cannot run before the signed ingress is configured on its own API."""

import pytest

from scheduling.workers.sms_outbox import require_live_sms_authorization


def test_sender_requires_enabled_ingress_and_exact_urls() -> None:
    base = "https://api.example.test"
    ready = {
        "SMS_SEND_ENABLED": "authorized",
        "SMS_API_BASE_URL": base,
        "TWILIO_INBOUND_URL": f"{base}/webhooks/sms/inbound",
        "TWILIO_STATUS_URL": f"{base}/webhooks/sms/status",
    }
    require_live_sms_authorization(ready)
    for change in (
        {"SMS_SEND_ENABLED": "disabled"},
        {"SMS_API_BASE_URL": ""},
        {"TWILIO_INBOUND_URL": "https://example.invalid/webhooks/sms/inbound"},
        {"TWILIO_STATUS_URL": "https://example.invalid/webhooks/sms/status"},
    ):
        with pytest.raises(RuntimeError):
            require_live_sms_authorization(ready | change)
