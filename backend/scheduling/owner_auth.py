"""Verify Cognito access tokens for the single owner and business pilot."""

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

import jwt
from fastapi import FastAPI
from jwt import PyJWKClient

from scheduling.adapters.dynamodb import DynamoClient
from scheduling.owner_api import OwnerPrincipal, create_persisted_owner_app


class SigningKeys(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class CognitoOwnerTokenVerifier:
    """Trust only one configured owner subject in one configured user pool."""

    def __init__(
        self,
        issuer: str,
        client_id: str,
        owner_sub: str,
        business_id: str,
        signing_keys: SigningKeys | None = None,
    ) -> None:
        if not all((issuer.startswith("https://"), client_id, owner_sub, business_id)):
            raise ValueError("Cognito issuer, app client, owner and business are required")
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._owner_sub = owner_sub
        self._business_id = business_id
        self._keys = signing_keys or PyJWKClient(f"{self._issuer}/.well-known/jwks.json")

    def __call__(self, token: str) -> OwnerPrincipal:
        key = self._keys.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=self._issuer,
            options={"verify_aud": False, "require": ["exp", "iat", "iss", "sub"]},
        )
        if "aud" in claims:
            raise ValueError("Resource-bound access tokens require an expected API audience")
        if (claims.get("token_use") != "access"
                or claims.get("client_id") != self._client_id
                or claims.get("sub") != self._owner_sub):
            raise ValueError("Token is not authorized for the pilot owner")
        return OwnerPrincipal(self._owner_sub, self._business_id)


def create_cognito_owner_app(
    client: DynamoClient,
    table_name: str,
    issuer: str,
    client_id: str,
    owner_sub: str,
    business_id: str,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Construct the persisted API with a required, validated Cognito owner token."""
    verifier = CognitoOwnerTokenVerifier(issuer, client_id, owner_sub, business_id)
    return create_persisted_owner_app(client, table_name, verifier, clock)
