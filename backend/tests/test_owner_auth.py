"""Cognito tokens must be signed, current, and issued to the configured owner."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from scheduling.owner_auth import CognitoOwnerTokenVerifier

ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_pilot"


def test_cognito_verifier_rejects_wrong_claims_and_signature() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    another_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Keys:
        def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
            return SimpleNamespace(key=private_key.public_key())

    verifier = CognitoOwnerTokenVerifier(ISSUER, "client-1", "owner-sub", "pilot", Keys())
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER, "sub": "owner-sub", "client_id": "client-1",
        "token_use": "access", "iat": now, "exp": now + timedelta(minutes=5),
    }

    def signed(overrides: dict[str, object] | None = None, key: object = private_key) -> str:
        return jwt.encode({**claims, **(overrides or {})}, key, algorithm="RS256")

    assert verifier(signed()).actor_id == "owner-sub"
    for override in (
        {"sub": "other-owner"}, {"client_id": "other-client"},
        {"token_use": "id"}, {"iss": "https://other.example"},
        {"aud": "https://other-api.example"},
        {"exp": now - timedelta(minutes=1)},
    ):
        with pytest.raises((ValueError, jwt.PyJWTError)):
            verifier(signed(override))
    with pytest.raises(jwt.PyJWTError):
        verifier(signed(key=another_key))
