"""The webhook and sender resolve one scoped encrypted token at runtime."""

import pytest

from scheduling.adapters.twilio_secret import load_twilio_auth_token


class FakeSsm:
    def __init__(self, value: str) -> None:
        self.value = value
        self.calls: list[dict[str, object]] = []

    def get_parameter(self, **kwargs: object) -> dict[str, dict[str, str]]:
        self.calls.append(kwargs)
        return {"Parameter": {"Value": self.value}}


def test_loads_encrypted_parameter_by_exact_name() -> None:
    client = FakeSsm("synthetic-token")
    assert load_twilio_auth_token(client, "/scheduling/dev/twilio/auth-token") == "synthetic-token"
    assert client.calls == [{"Name": "/scheduling/dev/twilio/auth-token", "WithDecryption": True}]


def test_rejects_unscoped_or_empty_token() -> None:
    client = FakeSsm("")
    with pytest.raises(ValueError, match="under /scheduling/"):
        load_twilio_auth_token(client, "/other/token")
    assert client.calls == []
    with pytest.raises(ValueError, match="empty"):
        load_twilio_auth_token(client, "/scheduling/dev/twilio/auth-token")
