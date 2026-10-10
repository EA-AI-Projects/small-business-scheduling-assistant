"""Every client route the backend mounts is routed through API Gateway behind the JWT authorizer."""

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import yaml

from scheduling.linked_auth import create_cognito_linked_app

TEMPLATE = Path(__file__).resolve().parents[2] / "template.yaml"


def _loader() -> type[yaml.SafeLoader]:
    class Loader(yaml.SafeLoader):
        pass

    def construct(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
        if isinstance(node, yaml.ScalarNode):
            return {suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {suffix: loader.construct_sequence(node, deep=True)}
        return {suffix: loader.construct_mapping(node, deep=True)}  # type: ignore[arg-type]

    Loader.add_multi_constructor("!", construct)
    return Loader


def _template() -> dict[str, Any]:
    return yaml.load(TEMPLATE.read_text(), Loader=_loader())


def _mounted_routes() -> set[tuple[str, str]]:
    """Every /v1 route the deployed app mounts outside the owner proxy."""
    app = create_cognito_linked_app(
        MagicMock(), "table", "https://cognito-idp.us-east-1.amazonaws.com/pool_1",
        "client-id", cognito_client=MagicMock())
    return {
        (method, route.path)  # type: ignore[attr-defined]
        for route in app.routes
        for method in (getattr(route, "methods", None) or ())
        if route.path.startswith("/v1/")  # type: ignore[attr-defined]
        and not route.path.startswith("/v1/owner/")  # type: ignore[attr-defined]
    }


def _gateway_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _is_public_prefix(path: str) -> bool:
    return path.startswith(("/v1/client/", "/v1/account/"))


def _gateway_routes() -> list[tuple[str, str, str, Any, str]]:
    """(method, path, resource, api, authorizer) for every HttpApi event or Route."""
    found: list[tuple[str, str, str, Any, str]] = []
    for name, res in _template()["Resources"].items():
        if res["Type"] == "AWS::Serverless::Function":
            for event in (res["Properties"].get("Events") or {}).values():
                if event["Type"] != "HttpApi":
                    continue
                p = event["Properties"]
                auth = (p.get("Auth") or {}).get("Authorizer", "NONE")
                found.append((p["Method"], _gateway_path(p["Path"]), name,
                              p.get("ApiId"), auth))
        elif res["Type"] == "AWS::ApiGatewayV2::Route":
            key = res["Properties"]["RouteKey"]
            method, path = key.split(" ", 1)
            found.append((method, _gateway_path(path), name,
                          res["Properties"].get("ApiId"),
                          res["Properties"].get("AuthorizationType", "NONE")))
    return found


def test_mounted_non_owner_routes_are_the_expected_set() -> None:
    assert _mounted_routes() == {
        ("POST", "/v1/account/invitations/{business_id}/{client_id}/activate"),
        ("POST", "/v1/account/invitations/activate"),
        ("GET", "/v1/client/session"),
        ("GET", "/v1/client/availability"),
        ("GET", "/v1/client/bookings"),
        ("POST", "/v1/client/requests"),
        ("POST", "/v1/client/bookings/{appointment_id}/cancel"),
        ("POST", "/v1/client/bookings/{appointment_id}/reschedule"),
    }


def test_every_mounted_route_is_routed_through_the_api() -> None:
    routed = {(m, p) for m, p, *_ in _gateway_routes()}
    missing = {r for r in _mounted_routes() if (r[0], _gateway_path(r[1])) not in routed}
    assert not missing, f"mounted routes not in template.yaml: {sorted(missing)}"


def test_every_client_and_account_route_uses_the_jwt_authorizer_everywhere() -> None:
    """Any function or route resource exposing these paths must use OwnerJwt on OwnerHttpApi."""
    seen = 0
    for method, path, name, api, auth in _gateway_routes():
        if not _is_public_prefix(path):
            continue
        seen += 1
        assert api == {"Ref": "OwnerHttpApi"}, (name, method, path)
        assert auth == "OwnerJwt", (name, method, path, auth)
    assert seen >= len(_mounted_routes())
    assert "OwnerJwt" in _template()["Resources"]["OwnerHttpApi"]["Properties"]["Auth"]["Authorizers"]


def test_cors_preflight_allows_idempotency_key_and_post() -> None:
    cors = _template()["Resources"]["OwnerHttpApi"]["Properties"]["CorsConfiguration"]
    assert "idempotency-key" in {h.lower() for h in cors["AllowHeaders"]}
    assert {"POST", "GET", "OPTIONS"} <= set(cors["AllowMethods"])
