"""Cognito access-token verification and role-scoped API construction."""

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

import jwt
from fastapi import FastAPI
from jwt import PyJWKClient

from scheduling.account_invitations import ClientAccountInvitations
from scheduling.adapters.account_invitations_aws import (
    CognitoAccountDirectory,
    DynamoClientInvitations,
)
from scheduling.adapters.dynamodb import DynamoClient, DynamoDBCalendarRepository
from scheduling.client_api import add_client_portal_routes, add_client_session_route
from scheduling.identity_links import DynamoIdentityLinkStore, IdentityLink, IdentityLinks, LinkRole
from scheduling.owner_api import OwnerPrincipal, create_persisted_owner_app
from scheduling.owner_auth import CognitoVerifiedAccountVerifier


class SigningKeys(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class CognitoLinkedTokenVerifier:
    """Validate a Cognito access token, then resolve its active record link."""

    def __init__(self, issuer: str, client_id: str, links: IdentityLinks,
                 signing_keys: SigningKeys | None = None) -> None:
        if not issuer.startswith("https://") or not client_id:
            raise ValueError("Cognito issuer and app client are required")
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._links = links
        self._keys = signing_keys or PyJWKClient(f"{self._issuer}/.well-known/jwks.json")

    def __call__(self, token: str) -> IdentityLink:
        key = self._keys.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"], issuer=self._issuer,
            options={"verify_aud": False, "require": ["exp", "iat", "iss", "sub"]},
        )
        if "aud" in claims:
            raise ValueError("Resource-bound access tokens require an expected API audience")
        if (claims.get("token_use") != "access"
                or claims.get("client_id") != self._client_id):
            raise ValueError("Token is not an access token for this app client")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("Token subject is missing")
        link = self._links.resolve(subject)
        if link is None:
            raise ValueError("Active identity link is missing or invalid")
        return link


def create_cognito_linked_app(
    client: DynamoClient, table_name: str, issuer: str, client_id: str,
    clock: Callable[[], datetime] | None = None, *,
    cors_origins: tuple[str, ...] = (),
    cognito_client: Any | None = None,
) -> FastAPI:
    """Mount owner and client routes behind the same verified link resolver."""
    records = DynamoDBCalendarRepository(client, table_name)
    links = IdentityLinks(DynamoIdentityLinkStore(client, table_name, records))
    verify = CognitoLinkedTokenVerifier(issuer, client_id, links)

    def owner(token: str) -> OwnerPrincipal:
        link = verify(token)
        if link.role != LinkRole.OWNER:
            raise ValueError("Owner access is required")
        return OwnerPrincipal(link.subject, link.business_id)

    invitations = None
    verify_account = None
    if cognito_client is not None:
        pool_id = issuer.rstrip("/").rsplit("/", 1)[-1]
        invitations = ClientAccountInvitations(
            DynamoClientInvitations(client, table_name, records), links,
            CognitoAccountDirectory(cognito_client, pool_id))
        verify_account = CognitoVerifiedAccountVerifier(issuer, client_id)
    app = create_persisted_owner_app(
        client, table_name, owner, clock, cors_origins=cors_origins,
        account_invitations=invitations, verify_account=verify_account)
    add_client_session_route(app, verify)
    add_client_portal_routes(app, verify, records, clock)
    return app
