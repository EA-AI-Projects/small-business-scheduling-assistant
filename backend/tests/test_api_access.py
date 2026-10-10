"""Direct HTTP requests enforce verified owner and client record boundaries."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from scheduling.account_invitations import ClientAccountInvitations, VerifiedAccount
from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.client_api import add_client_session_route
from scheduling.identity_links import IdentityLink, IdentityLinks, LinkRole, LinkState
from scheduling.linked_auth import CognitoLinkedTokenVerifier, create_cognito_linked_app
from scheduling.owner_api import OwnerPrincipal, create_owner_app
from scheduling.owner_auth import CognitoVerifiedAccountVerifier

ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_synthetic"
NOW = datetime.now(UTC)
OWNER_URL = "/v1/owner/businesses/pilot/clients"
CLIENT_URL = "/v1/client/session"


class Links:
    def __init__(self) -> None:
        self.subjects: dict[str, IdentityLink] = {}
        self.targets: dict[tuple[LinkRole, str, str | None], str] = {}
        self.profiles: set[tuple[str, str]] = {("pilot", "client-1")}
        self.businesses: set[str] = {"pilot"}

    def add(self, subject: str, role: LinkRole, *, client_id: str | None = None,
            state: LinkState = LinkState.ACTIVE) -> None:
        self.subjects[subject] = IdentityLink(subject, role, "pilot", client_id,
                                              state, 1, NOW, "synthetic-admin", "test")
        self.targets[(role, "pilot", client_id)] = subject

    def read_link(self, subject: str) -> IdentityLink | None:
        return self.subjects.get(subject)

    def read_target_subject(self, role: LinkRole, business_id: str,
                            client_id: str | None) -> str | None:
        return self.targets.get((role, business_id, client_id))

    def read_profile(self, business_id: str, client_id: str) -> object | None:
        return object() if (business_id, client_id) in self.profiles else None

    def read_policy_record(self, business_id: str) -> object | None:
        return object() if business_id in self.businesses else None

    def write_link(self, link: IdentityLink, previous: IdentityLink | None) -> None:
        raise AssertionError("HTTP reads must never change links")


def setup() -> tuple[TestClient, Links, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Keys:
        def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
            return SimpleNamespace(key=private_key.public_key())

    store = Links()
    store.add("owner-sub", LinkRole.OWNER)
    store.add("client-sub", LinkRole.CLIENT, client_id="client-1")
    verifier = CognitoLinkedTokenVerifier(
        ISSUER, "app-client", IdentityLinks(store), Keys())  # type: ignore[arg-type]

    def owner(token: str) -> OwnerPrincipal:
        link = verifier(token)
        if link.role != LinkRole.OWNER:
            raise ValueError("Owner access is required")
        return OwnerPrincipal(link.subject, link.business_id)

    app = create_owner_app(InMemoryCalendarRepository(), owner)
    add_client_session_route(app, verifier)
    return TestClient(app), store, private_key


def token(key: rsa.RSAPrivateKey, subject: str, **overrides: object) -> str:
    claims = {"iss": ISSUER, "sub": subject, "client_id": "app-client",
              "token_use": "access", "iat": NOW, "exp": NOW + timedelta(minutes=5),
              **overrides}
    return jwt.encode(claims, key, algorithm="RS256")


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def test_direct_owner_and_client_requests_follow_only_the_verified_link() -> None:
    api, links, key = setup()
    owner = bearer(token(key, "owner-sub"))
    client = bearer(token(key, "client-sub"))

    assert api.get(OWNER_URL, headers=owner).status_code == 200
    assert api.get(CLIENT_URL, headers=client).json() == {
        "role": "client", "business_id": "pilot", "client_id": "client-1"}
    assert api.get(OWNER_URL, headers=client).status_code == 401
    assert api.get(CLIENT_URL, headers=owner).status_code == 403
    assert api.get(OWNER_URL.replace("pilot", "other"), headers=owner).status_code == 403
    assert api.get(CLIENT_URL + "?client_id=other", headers=client).json()["client_id"] == "client-1"
    assert api.get(CLIENT_URL, headers=client).json().keys() == {
        "role", "business_id", "client_id"}
    links.profiles.add(("pilot", "client-2"))
    links.add("other-client-sub", LinkRole.CLIENT, client_id="client-2")
    other_client = bearer(token(key, "other-client-sub"))
    assert api.get(CLIENT_URL, headers=other_client).json()["client_id"] == "client-2"
    assert api.get(CLIENT_URL + "?client_id=client-1", headers=other_client).json()[
        "client_id"] == "client-2"


def test_missing_pending_revoked_deleted_and_mismatched_links_are_denied() -> None:
    api, links, key = setup()
    assert api.get(OWNER_URL).status_code == 401
    assert api.get(CLIENT_URL).status_code == 401
    assert api.get(CLIENT_URL, headers=bearer(token(key, "unlinked"))).status_code == 401

    for state in (LinkState.PENDING, LinkState.REVOKED):
        links.add("client-sub", LinkRole.CLIENT, client_id="client-1", state=state)
        assert api.get(CLIENT_URL, headers=bearer(token(key, "client-sub"))).status_code == 401
    links.add("client-sub", LinkRole.CLIENT, client_id="client-1")
    links.targets[(LinkRole.CLIENT, "pilot", "client-1")] = "other-sub"
    assert api.get(CLIENT_URL, headers=bearer(token(key, "client-sub"))).status_code == 401
    links.targets[(LinkRole.CLIENT, "pilot", "client-1")] = "client-sub"
    links.profiles.clear()
    assert api.get(CLIENT_URL, headers=bearer(token(key, "client-sub"))).status_code == 401

    links.add("owner-sub", LinkRole.OWNER, state=LinkState.REVOKED)
    assert api.get(OWNER_URL, headers=bearer(token(key, "owner-sub"))).status_code == 401
    links.add("owner-sub", LinkRole.OWNER, state=LinkState.PENDING)
    assert api.get(OWNER_URL, headers=bearer(token(key, "owner-sub"))).status_code == 401
    links.add("owner-sub", LinkRole.OWNER)
    links.businesses.clear()
    assert api.get(OWNER_URL, headers=bearer(token(key, "owner-sub"))).status_code == 401


def test_invalid_token_claims_and_signatures_never_reach_either_route() -> None:
    api, _, key = setup()
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    invalid = [
        token(key, "client-sub", token_use="id"),
        token(key, "client-sub", client_id="other-app"),
        token(key, "client-sub", iss="https://wrong.example"),
        token(key, "client-sub", exp=NOW - timedelta(minutes=1)),
        token(key, "client-sub", aud="other-api"),
        token(wrong_key, "client-sub"),
    ]
    for bad_token in invalid:
        assert api.get(CLIENT_URL, headers=bearer(bad_token)).status_code == 401
        assert api.get(OWNER_URL, headers=bearer(bad_token)).status_code == 401


def test_deployed_app_wires_client_activation_with_role_scoped_routes(monkeypatch) -> None:
    activated: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        CognitoVerifiedAccountVerifier, "__call__",
        lambda self, token: VerifiedAccount("client-sub", "client@example.test", True),
    )

    def record_activation(self, business_id: str, client_id: str,
                          account: VerifiedAccount, now: datetime) -> None:
        activated.append((business_id, client_id, account.subject))

    monkeypatch.setattr(ClientAccountInvitations, "activate", record_activation)
    app = create_cognito_linked_app(
        object(), "test-table", ISSUER, "app-client", cognito_client=object())  # type: ignore[arg-type]
    api = TestClient(app)
    response = api.post("/v1/account/invitations/pilot/client-1/activate",
                        headers=bearer("synthetic-id-token"))
    assert response.status_code == 200
    assert activated == [("pilot", "client-1", "client-sub")]
    assert any(route.path == CLIENT_URL for route in app.routes)
