"""Verify Cognito access tokens and resolve the pilot owner's explicit link."""

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

import jwt
from fastapi import FastAPI
from jwt import PyJWKClient

from scheduling.account_invitations import ClientAccountInvitations, VerifiedAccount
from scheduling.adapters.account_invitations_aws import (
    CognitoAccountDirectory,
    DynamoClientInvitations,
)
from scheduling.adapters.dynamodb import DynamoClient
from scheduling.identity_links import IdentityLinks, LinkRole
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
        links: IdentityLinks | None = None,
    ) -> None:
        if not all((issuer.startswith("https://"), client_id, owner_sub, business_id)):
            raise ValueError("Cognito issuer, app client, owner and business are required")
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._owner_sub = owner_sub
        self._business_id = business_id
        self._links = links
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
        if self._links is not None:
            link = self._links.resolve(self._owner_sub)
            if (link is None or link.role != LinkRole.OWNER
                    or link.business_id != self._business_id):
                raise ValueError("Pilot owner link is missing or invalid")
        return OwnerPrincipal(self._owner_sub, self._business_id)


class CognitoVerifiedAccountVerifier:
    """Verify the ID token's subject and verified email for client activation."""

    def __init__(self, issuer: str, client_id: str,
                 signing_keys: SigningKeys | None = None) -> None:
        if not issuer.startswith("https://") or not client_id:
            raise ValueError("Cognito issuer and client are required")
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._keys = signing_keys or PyJWKClient(f"{self._issuer}/.well-known/jwks.json")

    def __call__(self, token: str) -> VerifiedAccount:
        key = self._keys.get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"],
                            issuer=self._issuer, audience=self._client_id,
                            options={"require": ["exp", "iat", "iss", "sub", "aud"]})
        if claims.get("token_use") != "id":
            raise ValueError("Verified account requires an ID token")
        subject = claims.get("sub")
        email = claims.get("email")
        if (not isinstance(subject, str) or not subject or not isinstance(email, str)
                or not email or claims.get("email_verified") is not True):
            raise ValueError("Verified account email is required")
        return VerifiedAccount(subject, email, True)


def create_cognito_owner_app(
    client: DynamoClient,
    table_name: str,
    issuer: str,
    client_id: str,
    owner_sub: str,
    business_id: str,
    clock: Callable[[], datetime] | None = None,
    *,
    cors_origins: tuple[str, ...] = (),
    cognito_client: Any | None = None,
) -> FastAPI:
    """Construct the persisted API with a required, validated Cognito owner token."""
    from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
    from scheduling.identity_links import DynamoIdentityLinkStore

    records = DynamoDBCalendarRepository(client, table_name)
    links = IdentityLinks(DynamoIdentityLinkStore(client, table_name, records))
    verifier = CognitoOwnerTokenVerifier(issuer, client_id, owner_sub, business_id,
                                         links=links)
    invitations = None
    verify_account = None
    if cognito_client is not None:
        pool_id = issuer.rstrip("/").rsplit("/", 1)[-1]
        invitations = ClientAccountInvitations(
            DynamoClientInvitations(client, table_name, records), links,
            CognitoAccountDirectory(cognito_client, pool_id))
        verify_account = CognitoVerifiedAccountVerifier(issuer, client_id)
    return create_persisted_owner_app(client, table_name, verifier, clock,
                                      account_invitations=invitations,
                                      verify_account=verify_account,
                                      cors_origins=cors_origins)
