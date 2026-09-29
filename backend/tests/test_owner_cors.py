"""Cross-origin owner API access is limited to the configured owner app origin."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.local_owner import LOCAL_APP_ORIGINS, create_local_owner_app
from scheduling.owner_api import OwnerPrincipal, create_owner_app, validate_cors_origin

APP_ORIGIN = "https://main.d1example.amplifyapp.com"
BASE = "/v1/owner/businesses/pilot"
TEMPLATE = Path(__file__).resolve().parents[2] / "template.yaml"


def verify(token: str) -> OwnerPrincipal:
    if token != "verified-owner":
        raise ValueError("Invalid token")
    return OwnerPrincipal("owner-1", "pilot")


def api(origins: tuple[str, ...] = (APP_ORIGIN,)) -> TestClient:
    return TestClient(create_owner_app(InMemoryCalendarRepository(), verify,
                                       cors_origins=origins))


def preflight(client: TestClient, origin: str, method: str = "POST") -> dict[str, str]:
    response = client.options(f"{BASE}/blocks", headers={
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "authorization, content-type, idempotency-key",
    })
    return dict(response.headers)


def test_preflight_from_allowed_origin_lists_methods_and_headers() -> None:
    response = api().options(f"{BASE}/blocks", headers={
        "Origin": APP_ORIGIN,
        "Access-Control-Request-Method": "DELETE",
        "Access-Control-Request-Headers": "authorization, content-type, idempotency-key",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == APP_ORIGIN
    methods = {m.strip() for m in response.headers["access-control-allow-methods"].split(",")}
    assert methods == {"GET", "POST", "PUT", "PATCH", "DELETE"}
    allowed = {h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")}
    assert {"authorization", "content-type", "idempotency-key"} <= allowed
    assert response.headers["access-control-max-age"] == "600"
    assert "access-control-allow-credentials" not in response.headers


def test_disallowed_origin_gets_no_allow_origin() -> None:
    client = api()
    assert "access-control-allow-origin" not in preflight(client, "https://evil.example")
    response = client.get(f"{BASE}/calendar", headers={
        "Origin": "https://evil.example", "Authorization": "Bearer verified-owner"})
    assert "access-control-allow-origin" not in response.headers


def test_simple_request_from_allowed_origin_still_requires_auth() -> None:
    client = api()
    unauthenticated = client.get(f"{BASE}/calendar", headers={"Origin": APP_ORIGIN})
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["access-control-allow-origin"] == APP_ORIGIN
    authed = client.get(f"{BASE}/calendar", headers={
        "Origin": APP_ORIGIN, "Authorization": "Bearer verified-owner"})
    assert authed.status_code == 200
    assert authed.headers["access-control-allow-origin"] == APP_ORIGIN
    assert "access-control-allow-credentials" not in authed.headers


def test_without_configured_origins_no_cors_headers_are_sent() -> None:
    client = api(())
    assert "access-control-allow-origin" not in preflight(client, APP_ORIGIN)


@pytest.mark.parametrize("origin", [
    "*",
    "https://*.amplifyapp.com",
    "https://app.example.com/",
    "https://app.example.com/owner",
    "https://app.example.com?x=1",
    "https://app.example.com#frag",
    "https://user@app.example.com",
    "http://app.example.com",
    "https://",
    "app.example.com",
    "https://App.Example.com",
    "https://app.example.com:443",
])
def test_origin_validation_rejects_non_exact_or_insecure_origins(origin: str) -> None:
    with pytest.raises(ValueError):
        validate_cors_origin(origin)
    with pytest.raises(ValueError):
        create_owner_app(InMemoryCalendarRepository(), verify, cors_origins=(origin,))


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://127.0.0.1:3000",
                                    "http://localhost"])
def test_loopback_http_requires_explicit_flag(origin: str) -> None:
    with pytest.raises(ValueError):
        validate_cors_origin(origin)
    assert validate_cors_origin(origin, allow_loopback_http=True) == origin


def test_loopback_flag_does_not_allow_other_http_hosts() -> None:
    with pytest.raises(ValueError):
        validate_cors_origin("http://192.168.1.10:3000", allow_loopback_http=True)


def test_https_origin_with_port_is_accepted() -> None:
    assert validate_cors_origin("https://app.example.com:8443") == "https://app.example.com:8443"


@pytest.mark.parametrize("token", [None, "", "short-token"])
def test_local_server_rejects_missing_or_short_token(token: str | None) -> None:
    with pytest.raises(RuntimeError, match="LOCAL_OWNER_TOKEN"):
        create_local_owner_app(token)


def test_local_server_accepts_only_configured_token_from_local_origins() -> None:
    token = "local-dev-token-0123456789"
    client = TestClient(create_local_owner_app(token))
    assert client.get(f"{BASE}/calendar").status_code == 401
    assert client.get(f"{BASE}/calendar", headers={
        "Authorization": "Bearer local-dev-token-wrong-000"}).status_code == 401
    ok = client.get(f"{BASE}/requests", headers={
        "Authorization": f"Bearer {token}", "Origin": LOCAL_APP_ORIGINS[0]})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == LOCAL_APP_ORIGINS[0]
    assert len(ok.json()) == 1
    clients = client.get(f"{BASE}/clients", headers={"Authorization": f"Bearer {token}"})
    assert all(c["phone_e164"].startswith("+1415555010") for c in clients.json())
    assert "access-control-allow-origin" not in preflight(client, "https://evil.example")
    # No SMS routes are mounted on the synthetic server.
    assert client.get(f"{BASE}/sms-delivery-failures", headers={
        "Authorization": f"Bearer {token}"}).status_code == 404


def _resource(text: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n((?:    .*\n|\n)+)", text, re.MULTILINE)
    assert match, name
    return match.group(1)


def test_template_wires_cors_and_owner_app_origin() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    parameter = _resource(text, "OwnerAppOrigin")
    match = re.search(r"AllowedPattern: '([^']+)'", parameter)
    assert match is not None
    # CloudFormation matches the whole value; keep it no looser than validate_cors_origin.
    pattern = re.compile(match.group(1))
    for origin in ("https://main.d1abc.amplifyapp.com", "https://owner.example.com:8443"):
        assert pattern.fullmatch(origin) and validate_cors_origin(origin) == origin
    for origin in ("https://App.Example.com", "https://app.example.com:443",
                   "https://app.example.com/", "https://app.example.com/owner",
                   "http://app.example.com", "https://*.amplifyapp.com"):
        assert pattern.fullmatch(origin) is None
        with pytest.raises(ValueError):
            validate_cors_origin(origin)
    api_block = _resource(text, "OwnerHttpApi")
    assert "CorsConfiguration:" in api_block
    assert "AllowOrigins: [!Ref OwnerAppOrigin]" in api_block
    assert "AllowMethods: [GET, POST, PUT, PATCH, DELETE, OPTIONS]" in api_block
    assert "AllowHeaders: [authorization, content-type, idempotency-key]" in api_block
    assert "MaxAge: 600" in api_block
    assert "AllowCredentials: false" in api_block
    assert "*" not in api_block.split("CorsConfiguration:")[1].split("Auth:")[0]
    pool_client = _resource(text, "OwnerUserPoolClient")
    assert "- !Ref OwnerRedirectUri" in pool_client
    assert "- !Sub '${OwnerAppOrigin}/'" in pool_client
    assert "LogoutURLs: [!Sub '${OwnerAppOrigin}/']" in pool_client
    function = _resource(text, "OwnerApiFunction")
    assert "OWNER_APP_ORIGIN: !Ref OwnerAppOrigin" in function


def test_owner_api_routes_leave_preflight_to_gateway_cors() -> None:
    """An authorized ANY/OPTIONS route would catch preflight and return 401."""
    function = _resource(TEMPLATE.read_text(encoding="utf-8"), "OwnerApiFunction")
    events = re.findall(
        r"^        (\w+):\n          Type: HttpApi\n          Properties:\n"
        r"((?:            .*\n)+)", function, re.MULTILINE)
    methods: dict[str, str] = {}
    for name, body in events:
        path = re.search(r"Path: (\S+)", body)
        method = re.search(r"Method: (\S+)", body)
        assert path and method, name
        if path.group(1) == "/v1/owner/{proxy+}":
            assert "Auth: {Authorizer: OwnerJwt}" in body, name
            methods[name] = method.group(1).upper()
    assert sorted(methods.values()) == sorted(["GET", "POST", "PUT", "PATCH", "DELETE"])
    assert not {"ANY", "OPTIONS"} & set(methods.values())
