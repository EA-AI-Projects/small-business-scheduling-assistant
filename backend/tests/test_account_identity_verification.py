"""Cognito claims and operator approval guard account activation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from scheduling.owner_auth import CognitoVerifiedAccountVerifier
from scheduling.tools.provision_approved_owner import approved_owner_subject

ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_synthetic"


def test_verified_email_id_token_is_required_for_client_activation() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Keys:
        def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
            return SimpleNamespace(key=private.public_key())

    verifier = CognitoVerifiedAccountVerifier(ISSUER, "client-id", Keys())
    now = datetime.now(UTC)
    claims = {"iss": ISSUER, "aud": "client-id", "sub": "client-sub",
              "token_use": "id", "email": "client@example.test",
              "email_verified": True, "iat": now, "exp": now + timedelta(minutes=5)}

    def signed(overrides: dict[str, object] | None = None) -> str:
        return jwt.encode({**claims, **(overrides or {})}, private, algorithm="RS256")

    assert verifier(signed()).email == "client@example.test"
    for changes in ({"email_verified": False}, {"email": ""},
                    {"token_use": "access"}, {"aud": "other"},
                    {"iss": "https://other.example"},
                    {"exp": now - timedelta(seconds=1)}):
        with pytest.raises((ValueError, jwt.PyJWTError)):
            verifier(signed(changes))


def test_operator_can_link_only_approved_verified_owner_identity() -> None:
    user = {"Enabled": True, "UserStatus": "CONFIRMED", "UserAttributes": [
        {"Name": "sub", "Value": "owner-sub"},
        {"Name": "email", "Value": "owner@example.test"},
        {"Name": "email_verified", "Value": "true"},
    ]}
    assert approved_owner_subject(user, "OWNER@example.test") == "owner-sub"
    for bad in ({**user, "Enabled": False},
                {**user, "UserStatus": "FORCE_CHANGE_PASSWORD"},
                {**user, "UserAttributes": user["UserAttributes"][:-1]}):
        with pytest.raises(ValueError):
            approved_owner_subject(bad, "owner@example.test")
    with pytest.raises(ValueError):
        approved_owner_subject(user, "other@example.test")
