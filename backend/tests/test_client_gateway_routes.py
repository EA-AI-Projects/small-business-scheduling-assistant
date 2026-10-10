"""Every client route the backend mounts is routed through API Gateway behind the JWT authorizer."""

import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.client_api import add_client_session_route

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


def _mounted_client_routes() -> set[tuple[str, str]]:
    app = FastAPI()

    def verify(token: str) -> Any:
        raise ValueError(token)

    add_client_session_route(app, verify, InMemoryCalendarRepository(), None)
    routes = {
        (method, route.path)  # type: ignore[attr-defined]
        for route in app.routes
        for method in (getattr(route, "methods", None) or ())
        if route.path.startswith("/v1/client/")  # type: ignore[attr-defined]
    }
    return routes


def _gateway_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _client_events() -> dict[tuple[str, str], dict[str, Any]]:
    resources = _template()["Resources"]
    events = resources["OwnerApiFunction"]["Properties"]["Events"]
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events.values():
        props = event["Properties"]
        if props["Path"].startswith("/v1/client/"):
            found[(props["Method"], _gateway_path(props["Path"]))] = props
    return found


def test_mounted_client_routes_are_the_expected_six() -> None:
    assert {(m, p) for m, p in _mounted_client_routes()} == {
        ("GET", "/v1/client/session"),
        ("GET", "/v1/client/availability"),
        ("GET", "/v1/client/bookings"),
        ("POST", "/v1/client/requests"),
        ("POST", "/v1/client/bookings/{appointment_id}/cancel"),
        ("POST", "/v1/client/bookings/{appointment_id}/reschedule"),
    }


def test_every_mounted_client_route_is_routed_through_the_api() -> None:
    routed = set(_client_events())
    missing = {r for r in _mounted_client_routes() if (r[0], _gateway_path(r[1])) not in routed}
    assert not missing, f"client routes not in template.yaml: {sorted(missing)}"


def test_every_client_route_uses_the_cognito_jwt_authorizer_on_the_owner_api() -> None:
    template = _template()["Resources"]
    api = template["OwnerHttpApi"]["Properties"]
    assert "OwnerJwt" in api["Auth"]["Authorizers"]
    for key, props in _client_events().items():
        assert props["ApiId"] == {"Ref": "OwnerHttpApi"}, key
        assert props["Auth"] == {"Authorizer": "OwnerJwt"}, key


def test_cors_preflight_allows_idempotency_key_and_post() -> None:
    cors = _template()["Resources"]["OwnerHttpApi"]["Properties"]["CorsConfiguration"]
    assert "idempotency-key" in {h.lower() for h in cors["AllowHeaders"]}
    assert {"POST", "GET", "OPTIONS"} <= set(cors["AllowMethods"])
