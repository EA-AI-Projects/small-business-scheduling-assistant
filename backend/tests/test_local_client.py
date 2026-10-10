"""The local harness serves the real client routes behind per-client synthetic tokens."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scheduling.local_owner import (
    create_local_owner_app,
    generate_client_tokens,
)

NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)
OWNER = "local-test-owner-token-0123456789"
CLIENT_TOKENS = {"client-1": "local-client-one-token-0123456", "client-2": "local-client-two-token-0123456"}
BASE = "/v1/owner/businesses/pilot"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def local() -> TestClient:
    return TestClient(create_local_owner_app(
        OWNER, clock=lambda: NOW, client_tokens=CLIENT_TOKENS))


def test_session_needs_a_valid_client_token_and_reports_the_business_zone() -> None:
    client = local()
    assert client.get("/v1/client/session").status_code == 401
    assert client.get("/v1/client/session", headers=bearer("wrong-token-0123456789")).status_code == 401
    session = client.get("/v1/client/session", headers=bearer(CLIENT_TOKENS["client-1"]))
    assert session.status_code == 200
    body = session.json()
    assert (body["role"], body["business_id"], body["client_id"]) == ("client", "pilot", "client-1")
    assert body["timezone"]


def test_owner_and_client_tokens_are_not_interchangeable() -> None:
    client = local()
    assert client.get("/v1/client/session", headers=bearer(OWNER)).status_code == 401
    assert client.get("/v1/client/bookings", headers=bearer(OWNER)).status_code == 401
    assert client.get(f"{BASE}/requests", headers=bearer(OWNER)).status_code == 200
    for token in CLIENT_TOKENS.values():
        assert client.get(f"{BASE}/requests", headers=bearer(token)).status_code == 401
        assert client.get("/local/texts/state", headers=bearer(token)).status_code == 401


def test_each_client_token_sees_only_its_own_bookings() -> None:
    client = local()
    first = client.get("/v1/client/bookings", headers=bearer(CLIENT_TOKENS["client-1"])).json()
    second = client.get("/v1/client/bookings", headers=bearer(CLIENT_TOKENS["client-2"])).json()
    assert [b["status"] for b in first["bookings"]] == ["CONFIRMED"]
    assert [b["status"] for b in second["bookings"]] == ["PENDING_APPROVAL"]
    other = second["bookings"][0]["appointment_id"]
    denied = client.post(f"/v1/client/bookings/{other}/cancel",
                         headers={**bearer(CLIENT_TOKENS["client-1"]), "Idempotency-Key": "x1"},
                         json={"expected_version": 1})
    assert denied.status_code == 404


def test_unverified_client_has_no_token() -> None:
    with pytest.raises(RuntimeError):
        create_local_owner_app(OWNER, clock=lambda: NOW,
                               client_tokens={**CLIENT_TOKENS, "client-3": "local-client-three-token-012"})


@pytest.mark.parametrize("tokens", [
    {"client-1": "short", "client-2": CLIENT_TOKENS["client-2"]},
    {"client-1": CLIENT_TOKENS["client-1"], "client-2": CLIENT_TOKENS["client-1"]},
    {"client-1": OWNER, "client-2": CLIENT_TOKENS["client-2"]},
])
def test_weak_duplicate_or_owner_client_tokens_are_refused(tokens: dict[str, str]) -> None:
    with pytest.raises(RuntimeError):
        create_local_owner_app(OWNER, clock=lambda: NOW, client_tokens=tokens)


def test_generated_tokens_are_distinct_and_long_enough() -> None:
    tokens = generate_client_tokens()
    assert set(tokens) == {"client-1", "client-2"}
    assert len(set(tokens.values())) == 2 and all(len(v) >= 16 for v in tokens.values())


def test_cors_allows_only_the_local_app_origins() -> None:
    client = local()
    for origin, allowed in (("http://127.0.0.1:3000", True), ("http://localhost:3000", True),
                            ("https://evil.example.test", False)):
        reply = client.options("/v1/client/session", headers={
            "Origin": origin, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization"})
        assert ("access-control-allow-origin" in reply.headers) is allowed, origin


def test_client_requests_owner_approves_client_sees_confirmed() -> None:
    client = local()
    auth = bearer(CLIENT_TOKENS["client-1"])
    day: Any = client.get("/v1/client/bookings", headers=auth).json()["bookings"][0]["start_at"][:10]
    times = client.get("/v1/client/availability", headers=auth, params={"day": day}).json()
    assert times["starts_at"]
    requested = client.post("/v1/client/requests", headers={**auth, "Idempotency-Key": "k1"},
                            json={"start_at": times["starts_at"][-1]})
    assert requested.status_code == 200, requested.text
    booking = requested.json()
    assert booking["status"] == "PENDING_APPROVAL"

    pending = [r for r in client.get(f"{BASE}/requests", headers=bearer(OWNER)).json()
               if r["appointment_id"] == booking["appointment_id"]]
    assert len(pending) == 1 and pending[0]["client_id"] == "client-1"
    approved = client.post(f"{BASE}/requests/{booking['appointment_id']}/approve",
                           headers={**bearer(OWNER), "Idempotency-Key": "a1"},
                           json={"expected_version": pending[0]["version"]})
    assert approved.status_code == 200, approved.text

    mine = client.get("/v1/client/bookings", headers=auth).json()["bookings"]
    assert {b["appointment_id"]: b["status"] for b in mine}[booking["appointment_id"]] == "CONFIRMED"
    # The owner decision also reaches the shared text simulator's notification feed.
    notes = client.get("/local/texts/state", headers=bearer(OWNER)).json()["messages"]
    assert any(m["party"] == "client-1" and "confirmed" in m["body"] for m in notes)


def test_deployed_modules_do_not_reach_the_local_harness() -> None:
    import pathlib
    root = pathlib.Path(__file__).parents[1] / "scheduling"
    for path in root.rglob("*.py"):
        if path.name in {"local_owner.py", "local_texts.py"}:
            continue
        assert "local_owner" not in path.read_text(), path


def test_module_app_is_built_once_so_printed_tokens_match(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from scheduling import local_owner
    monkeypatch.setenv("LOCAL_OWNER_TOKEN", OWNER)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(local_owner, "_built", None)
    first = local_owner.app  # type: ignore[attr-defined]
    assert local_owner.app is first  # type: ignore[attr-defined]
    printed = capsys.readouterr().out
    token = next(line.split(": ")[1] for line in printed.splitlines() if "client-1" in line)
    assert TestClient(first).get("/v1/client/session", headers=bearer(token)).status_code == 200
