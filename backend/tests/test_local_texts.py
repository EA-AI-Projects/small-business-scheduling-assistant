"""The local text simulator shares the synthetic owner API's calendar."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.conversation import MessageContext, MessageProposal
from scheduling.domain.owner_policy import OwnerPolicyService, PolicyCommand
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyContext,
    OwnerReplyIntent,
    OwnerReplyProposal,
)
from scheduling.local_owner import create_local_owner_app, seed_synthetic_data
from scheduling.local_texts import OfflineInterpreter, TextSimulator

NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)
TOKEN = "local-test-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/v1/owner/businesses/pilot"


class Unsafe:
    """A model that always proposes a booking; exact-command checks must still hold."""

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        self.calls += 1
        return MessageProposal("request_booking", None, "2026-10-05 09:00", None, False)


def local(interpreter: Unsafe | None = None) -> TestClient:
    return TestClient(create_local_owner_app(TOKEN, interpreter=interpreter,
                                             clock=lambda: NOW))


def text(client: TestClient, party: str, body: str) -> list[dict[str, str]]:
    response = client.post("/local/texts", headers=AUTH, json={"party": party, "body": body})
    assert response.status_code == 200, response.text
    messages: list[dict[str, str]] = response.json()["messages"]
    return messages


def bodies(messages: list[dict[str, str]], kind: str) -> list[str]:
    return [message["body"] for message in messages if message["kind"] == kind]


def test_page_is_public_but_state_and_sending_need_the_local_token() -> None:
    client = local()
    page = client.get("/local/texts")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/local/texts/state").status_code == 401
    assert client.get("/local/texts/state",
                      headers={"Authorization": "Bearer wrong-token-0123456789"}).status_code == 401
    assert client.post("/local/texts", json={"party": "owner", "body": "hi"}).status_code == 401
    assert "/local/texts" not in client.get("/openapi.json").text
    # One text at a time: Send stays disabled until the reply arrives.
    assert "if (sending) return;" in page.text
    assert 'id="send-button"' in page.text


def test_model_sees_only_its_own_recent_simulated_texts() -> None:
    class Reading(Unsafe):
        def __init__(self) -> None:
            super().__init__()
            self.client_histories: list[list[str]] = []
            self.owner_histories: list[list[str]] = []

        def propose(self, body: str, context: MessageContext) -> MessageProposal:
            self.client_histories.append([message.text for message in context.history])
            return MessageProposal("clarify", None, None, None, True)

        def classify_owner_reply(self, body: str,
                                 context: OwnerReplyContext) -> OwnerReplyProposal:
            self.owner_histories.append([message.text for message in context.history])
            return OwnerReplyProposal(OwnerReplyIntent.UNCLEAR, None, Confidence.HIGH)

    model = Reading()
    current = [NOW]
    client = TestClient(create_local_owner_app(TOKEN, interpreter=model,
                                               clock=lambda: current[0]))
    text(client, "client-1", "Could you come Monday morning?")
    assert model.client_histories[0][0] == "Could you come Monday morning?"
    first_reply = bodies(client.get("/local/texts/state", headers=AUTH).json()["messages"],
                         "reply")[0]
    current[0] += timedelta(seconds=1)
    text(client, "client-2", "Could you come Tuesday afternoon?")
    current[0] += timedelta(seconds=1)
    text(client, "client-1", "What about Tuesday?")
    assert model.client_histories[-1] == [
        "Could you come Monday morning?", first_reply, "What about Tuesday?"]
    current[0] += timedelta(hours=25)
    text(client, "client-1", "What about Wednesday?")
    assert model.client_histories[-1] == ["What about Wednesday?"]
    current[0] += timedelta(seconds=1)
    text(client, "owner", "What is waiting for me?")
    assert model.owner_histories[-1] == ["What is waiting for me?"]
    current[0] += timedelta(seconds=1)
    booked = text(client, "client-1", "Book 2026-10-05 09:00")
    client_notice = next(message["body"] for message in booked
                         if message["kind"] == "notification" and message["party"] == "client-1")
    owner_notice = next(message["body"] for message in booked
                        if message["kind"] == "notification" and message["party"] == "owner")
    current[0] += timedelta(seconds=1)
    text(client, "client-1", "What about Thursday?")
    assert client_notice in model.client_histories[-1]
    assert owner_notice not in model.client_histories[-1]


def test_text_booking_appears_in_owner_api_and_owner_app_approval_notifies_client() -> None:
    client = local()
    options = text(client, "client-1", "Book 2026-10-05")
    assert "1) 8:00 AM" in bodies(options, "reply")[0]
    assert client.get(f"{BASE}/requests", headers=AUTH).json()[0]["client_id"] == "client-2"

    booked = text(client, "client-1", "Book 2026-10-05 09:00")
    assert not bodies(booked, "reply")  # Production texts only the notifications.
    assert bodies(booked, "note")[0].startswith("Not texted: Requested Mon Oct 5 at 9:00 AM")
    notifications = bodies(booked, "notification")
    assert any(body.startswith("New cleaning request from Avery Example") for body in notifications)
    assert any("is pending owner approval" in body for body in notifications)
    pending = [request for request in client.get(f"{BASE}/requests", headers=AUTH).json()
               if request["client_id"] == "client-1"]
    assert len(pending) == 1

    request = pending[0]
    approved = client.post(f"{BASE}/requests/{request['appointment_id']}/approve",
                           headers={**AUTH, "Idempotency-Key": "approve-local-1"},
                           json={"expected_version": request["version"]})
    assert approved.status_code == 200, approved.text
    state = client.get("/local/texts/state", headers=AUTH).json()["messages"]
    confirmed = [message for message in state if message["kind"] == "notification"
                 and message["party"] == "client-1" and "confirmed" in message["body"]]
    assert len(confirmed) == 1
    assert request["appointment_id"][:8] in confirmed[0]["body"]


def test_owner_text_approval_updates_the_owner_api_calendar() -> None:
    client = local()
    request = client.get(f"{BASE}/requests", headers=AUTH).json()[0]
    reply = text(client, "owner", f"Approve {request['appointment_id'][:8]}")
    assert "confirmed" in bodies(reply, "note")[0]
    appointment = client.get(f"{BASE}/appointments/{request['appointment_id']}",
                             headers=AUTH).json()
    assert appointment["status"] == "CONFIRMED"
    assert any("confirmed" in body for body in bodies(reply, "notification"))


def test_unverified_client_and_ambiguous_text_change_nothing() -> None:
    model = Unsafe()
    client = local(model)
    before = client.get(f"{BASE}/calendar", headers=AUTH).json()
    ignored = text(client, "client-3", "Book 2026-10-05 09:00")
    assert [message["kind"] for message in ignored] == ["text", "note"]
    assert "production ignores the text" in ignored[1]["body"]
    unclear = text(client, "client-1", "Can you come Monday morning?")
    assert not bodies(unclear, "notification")
    assert model.calls == 1
    assert client.get(f"{BASE}/calendar", headers=AUTH).json() == before
    parties = client.get("/local/texts/state", headers=AUTH).json()["parties"]
    assert {party["id"]: party["can_text"] for party in parties} == {
        "owner": True, "client-1": True, "client-2": True, "client-3": False}


def test_offline_mode_prompts_for_commands_and_private_data_is_rejected() -> None:
    client = local()
    reply = text(client, "client-1", "Could you come by sometime next week?")
    assert "What day" in bodies(reply, "reply")[0]
    assert "need OPENAI_API_KEY" in bodies(reply, "note")[0]
    offered = text(client, "client-1", "Book 2026-10-05")
    assert not bodies(offered, "note")  # Exact commands do not consult the model.
    for body in ("Call me at 415-555-0199", "Gate code is 1234"):
        response = client.post("/local/texts", headers=AUTH,
                               json={"party": "client-1", "body": body})
        assert response.status_code == 422
    assert client.post("/local/texts", headers=AUTH,
                       json={"party": "client-9", "body": "Book 2026-10-05"}).status_code == 404
    logged = client.get("/local/texts/state", headers=AUTH).json()["messages"]
    assert all("555" not in message["body"] for message in logged)
    assert all("1234" not in message["body"] for message in logged)


def test_keyword_and_unverified_notifications_are_explained_not_sent() -> None:
    client = local()
    stop = text(client, "client-1", "STOP")
    assert [message["kind"] for message in stop] == ["text", "note"]
    assert "not simulated" in stop[1]["body"]
    revision = client.get(f"{BASE}/policy", headers=AUTH).json()["calendar_revision"]
    created = client.post(f"{BASE}/appointments",
                          headers={**AUTH, "Idempotency-Key": "manual-casey"},
                          json={"expected_revision": revision, "client_id": "client-3",
                                "start_at": "2026-10-06T16:00:00Z", "duration_minutes": 180})
    assert created.status_code == 200, created.text
    state = client.get("/local/texts/state", headers=AUTH).json()["messages"]
    casey = [message for message in state if message["party"] == "client-3"]
    assert [message["kind"] for message in casey] == ["note"]
    assert casey[0]["body"].startswith("Not sent: this client has no verified phone")


def test_unstop_is_a_simulated_keyword_that_runs_no_conversation() -> None:
    unsafe = Unsafe()
    client = local(unsafe)
    unstop = text(client, "client-1", "unstop")
    assert [message["kind"] for message in unstop] == ["text", "note"]
    assert "not simulated" in unstop[1]["body"]
    assert unsafe.calls == 0


def test_owner_policy_change_shows_the_owner_notification() -> None:
    repository = InMemoryCalendarRepository()
    seed_synthetic_data(repository, NOW)
    simulator = TextSimulator(repository, OfflineInterpreter(), "pilot", lambda: NOW)
    record = repository.read_policy_record("pilot")
    assert record is not None
    OwnerPolicyService(repository, lambda: NOW).apply(PolicyCommand(
        "pilot", "local-owner", "policy-edit", repository.read_revision("pilot"),
        record.version, record.policy))
    assert [message["body"] for message in simulator.messages()] == [
        "Scheduling policy updated. Check the current owner calendar."]


class Plain:
    """Resolves one plain-language question the way the real model would."""

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        if body == "Hi. Do you have availability for tomorrow?":
            return MessageProposal("availability", None, None, None, False,
                                   "2026-09-30", "2026-09-30")
        return MessageProposal("clarify", None, None, None, True)

    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal:
        """A plain yes with exactly one request pending is a decision."""
        if body.lower() == "yes" and len(context.pending) == 1:
            return OwnerReplyProposal(OwnerReplyIntent.APPROVE_NAMED_REQUEST,
                                      context.pending[0].ref, Confidence.HIGH,
                                      request_version=context.pending[0].version)
        return OwnerReplyProposal(OwnerReplyIntent.UNCLEAR, None, Confidence.HIGH)


def test_plain_language_booking_and_owner_yes_in_the_simulator() -> None:
    client = local(Plain())  # type: ignore[arg-type]
    # The seeded request is the only one pending, so a plain yes approves it.
    seeded = text(client, "owner", "Yes")
    assert bodies(seeded, "note")[0].startswith("Not texted: Approved: Blake Sample, ")

    offer = bodies(text(client, "client-1", "Hi. Do you have availability for tomorrow?"),
                   "reply")[0]
    assert offer.startswith("Open times on Wed Sep 30: 1) ")
    assert not any(request["client_id"] == "client-1"
                   for request in client.get(f"{BASE}/requests", headers=AUTH).json())
    first = offer.split("1) ", 1)[1].split(",", 1)[0]  # e.g. "10:00 AM"

    booked = text(client, "client-1", f"{first} works")
    assert bodies(booked, "note")[0].startswith(f"Not texted: Requested Wed Sep 30 at {first}")
    assert not bodies(booked, "reply")
    assert any(body.startswith("New cleaning request from Avery Example")
               for body in bodies(booked, "notification"))

    approved = text(client, "owner", "yes")
    assert bodies(approved, "note")[0].startswith(
        f"Not texted: Approved: Avery Example, Wed Sep 30 at {first}")
    assert any(f"Cleaning visit confirmed: Wed Sep 30 at {first}" in body
               for body in bodies(approved, "notification"))
    assert client.get(f"{BASE}/requests", headers=AUTH).json() == []
