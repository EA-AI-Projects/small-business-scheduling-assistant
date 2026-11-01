"""Owner SMS tools through the receipt, shared HTTP views and fake-provider delivery.

The model is scripted; these checks prove backend authorization, selection, transactions,
receipts and delivery effects, not a live model's natural-language interpretation.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
from test_booking_invitation_selection import BUSINESS, RUN
from test_owner_tool_loop import ScriptedModel
from test_sms_processing import Reader
from test_sms_reminder_settings import World, commit_counter

from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.domain.conversation import ConversationService
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.sms_ingress import Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

NAME = "Avery Sample"


class OwnerWorld(World):
    def __init__(self) -> None:
        super().__init__()
        self.rename(self.target, NAME)
        self.rename(self.other, "Blake Example")
        self.owner_model = ScriptedModel(lambda _: "Nothing changed.")
        self.chat = ConversationService(
            self.records, self.owner_model, HoldService(self.records),
            LifecycleService(self.records, lambda: self.clock[0]), self.consent,
            lambda: self.clock[0], "+14155559999")

    def rename(self, who: str, name: str) -> None:
        p = self.records.read_profile(BUSINESS, who)
        assert p is not None
        self.records.save_profile(replace(p, name=name, version=p.version + 1),
                                  p.version, p.phone_e164)

    def inbound(self, body: str, receipt_id: str | None = None) -> Any:
        return replace(super().receipt(body, receipt_id), role=SenderRole.OWNER,
                       sender="+14155559999", client_id=None)

    def owner_text(self, body: str, action: Any, receipt_id: str | None = None) -> Any:
        self.owner_model.action = action
        return self.chat.handle(self.inbound(body, receipt_id))


def read(tool: Any, name: str = NAME, selection: str | None = None) -> dict[str, Any]:
    return tool("get_client_reminder_preference", {"client_name": name, "client_id": selection})


def change(tool: Any, *, enabled: bool | None = None, days: int | None = None,
           clock: str | None = None, name: str = NAME) -> dict[str, Any]:
    found = read(tool, name)
    assert found["ok"], found
    return tool("set_client_reminder_preference", {
        "client_id": found["client_id"], "version": found["version"],
        "enabled": enabled, "days_before": days, "local_time": clock})


def test_enrollment_partial_changes_receipts_and_shared_views() -> None:
    w = OwnerWorld()
    result = w.owner_text("Avery Sample asked for reminders two days before at 9 AM",
                         lambda t: (change(t, enabled=True, days=2, clock="09:00"), "Saved!")[1])
    assert w.saved() == (True, 2, "09:00:00")
    assert "reminders on" in result.text and "2 days before at 9:00 AM America/Los_Angeles" in result.text
    assert "0501" in result.text and "Only reminder settings" in result.text
    w.owner_text("Change Avery Sample's reminders to 11 AM",
                 lambda t: (change(t, clock="11:00"), "ok")[1])
    assert w.saved() == (True, 2, "11:00:00")
    w.owner_text("Avery Sample asked to turn reminders off",
                 lambda t: (change(t, enabled=False), "ok")[1])
    assert w.saved() == (False, 2, "11:00:00")
    w.owner_text("Change Avery Sample's reminder to three days before",
                 lambda t: (change(t, days=3), "ok")[1])
    assert w.saved() == (False, 3, "11:00:00")
    assert w.saved(w.other) == (False, 1, "10:00:00")
    # A later portal change is immediately visible in the owner SMS read.
    version = w.client_view()["version"]
    assert w.api.put("/v1/client/reminder-preference", headers={"Authorization": f"Bearer {w.target}"},
                     json={"expected_version": version, "days_before": 4}).status_code == 200
    def current(t: Any) -> str:
        assert read(t)["days_before"] == 4
        return "Four days before."
    assert w.owner_text("What are Avery Sample's reminder settings?", current).text == "Four days before."


def test_owner_sms_adapter_executes_strict_named_client_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    w = OwnerWorld()
    payloads: list[dict[str, Any]] = []
    def fake_model(request: Any, timeout: int) -> BytesIO:
        payload = json.loads(request.data)
        payloads.append(payload)
        if len(payloads) == 1:
            exported = {t["name"]: t for t in payload["tools"]}
            assert exported["get_client_reminder_preference"]["strict"]
            assert exported["set_client_reminder_preference"]["strict"]
            name = "get_client_reminder_preference"
            args = {"client_name": NAME, "client_id": None}
        elif len(payloads) == 2:
            found = json.loads(payload["input"][-1]["output"])
            name = "set_client_reminder_preference"
            args = {"client_id": found["client_id"], "version": found["version"],
                    "enabled": True, "days_before": 2, "local_time": "09:00"}
        else:
            assert json.loads(payload["input"][-1]["output"])["reminder_preference_saved"]
            return BytesIO(json.dumps({"output": [{"type": "message", "content": [
                {"type": "output_text", "text": "Saved seven days before at noon."}]}]}).encode())
        return BytesIO(json.dumps({"output": [{"type": "function_call", "name": name,
            "call_id": f"call-{len(payloads)}", "arguments": json.dumps(args)}]}).encode())
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_model)
    w.chat = ConversationService(
        w.records, OpenAIMessageInterpreter("synthetic-key"), HoldService(w.records),
        LifecycleService(w.records, lambda: w.clock[0]), w.consent,
        lambda: w.clock[0], "+14155559999")
    result = w.chat.handle(w.inbound("Avery Sample asked for reminders two days before at 9 AM"))
    assert w.saved() == (True, 2, "09:00:00") and len(payloads) == 3
    assert "2 days before at 9:00 AM" in result.text and "seven days" not in result.text


def test_matching_names_require_clarification_and_explicit_phone_pick() -> None:
    w = OwnerWorld()
    w.rename(w.other, NAME)
    before = w.owner_view()

    def ambiguous(t: Any) -> str:
        found = read(t)
        assert found["error"] == "client_needs_clarification" and len(found["candidates"]) == 2
        # The model cannot arbitrarily pick a duplicate even after seeing candidates.
        assert read(t, selection=w.target)["error"] == "client_needs_clarification"
        failure = t("set_client_reminder_preference", {"client_id": w.target,
            "version": before["version"], "enabled": True, "days_before": None, "local_time": None})
        assert failure["error"] == "client_not_resolved"
        return "Saved!"

    assert "did not change" in w.owner_text("Avery Sample asked for reminders", ambiguous).text
    assert w.owner_view() == before

    def select(t: Any) -> str:
        found = read(t, selection=w.target)
        assert found["ok"]
        assert t("set_client_reminder_preference", {"client_id": found["client_id"],
            "version": found["version"], "enabled": True, "days_before": None, "local_time": "09:00"})["ok"]
        return "ok"

    w.owner_text("Avery Sample with phone ending 0501 asked for reminders at 9 AM", select)
    assert w.saved() == (True, 1, "09:00:00")
    assert not w.owner_view(w.other)["enabled"]
    w.owner_text("What are Avery's reminders?", lambda t: (
        "Which full name?" if read(t, "Avery")["error"] == "client_needs_clarification" else "bad"))


@pytest.mark.parametrize("body,values", [
    ("Change Avery Sample to 9", {"clock": "09:00"}),
    ("YES", {"enabled": True}),
    ("Avery Sample asked for reminders 8 days before", {"days": 8}),
    ("Avery Sample asked for reminders at 7 AM", {"clock": "07:00"}),
    ("Avery Sample asked for reminders at 6:01 PM", {"clock": "18:01"}),
    ("Change Avery Sample to 9 PM", {"clock": "09:00"}),
])
def test_invalid_or_ambiguous_edits_change_nothing(body: str, values: dict[str, Any]) -> None:
    w = OwnerWorld()
    before = w.owner_view()
    calls = commit_counter(w)
    result = w.owner_text(body, lambda t: (change(t, **values), "Saved!")[1])
    assert "did not change" in result.text and "Saved!" not in result.text
    assert w.owner_view() == before and calls == []


def test_owner_sender_authorization_blocks_all_reads_and_writes() -> None:
    for restriction in ("unauthorized", "other_phone", "client_role", "stop", "opted_out"):
        w = OwnerWorld()
        receipt = w.inbound("Avery Sample asked for reminders at 9 AM")
        if restriction == "unauthorized":
            receipt = replace(receipt, authorized_for_commands=False)
        elif restriction == "other_phone":
            receipt = replace(receipt, sender="+14155550109")
        elif restriction == "client_role":
            receipt = replace(receipt, role=SenderRole.CLIENT, client_id=w.target)
        elif restriction == "stop":
            receipt = replace(receipt, keyword=Keyword.STOP)
        else:
            w.consent.stopped.add(f"{BUSINESS}:+14155559999")
        w.owner_model.action = lambda t: (change(t, enabled=True), "Saved!")[1]
        w.chat.handle(receipt)
        assert w.owner_model.calls == 0 and not w.owner_view()["enabled"]


def test_unknown_and_other_business_clients_are_not_exposed() -> None:
    w = OwnerWorld()
    profile = w.records.read_profile(BUSINESS, w.target)
    assert profile is not None
    w.records.save_profile(replace(profile, business_id="other-business", client_id="foreign",
                                   name="Foreign Person", version=1), 0, None)
    def query(t: Any) -> str:
        found = read(t, "Foreign Person")
        assert found["error"] == "client_needs_clarification" and found["candidates"] == []
        assert t("get_client_reminder_preference", {"client_name": NAME, "client_id": None,
                                                   "business_id": "other-business"})["error"] == "invalid_arguments"
        return "No matching client."
    w.owner_text("What are Foreign Person's reminder settings?", query)


def test_stale_write_does_not_overwrite_portal_change_or_retry_in_same_message() -> None:
    w = OwnerWorld()
    def race(t: Any) -> str:
        found = read(t)
        w.owner_edit(enabled=True, days_before=6, local_time="15:00")
        args = {"client_id": found["client_id"], "version": found["version"],
                "enabled": None, "days_before": None, "local_time": "09:00"}
        assert t("set_client_reminder_preference", args)["error"] == "stale_version"
        args["version"] = read(t)["version"]
        assert t("set_client_reminder_preference", args)["error"] == "action_already_attempted"
        return "Saved!"
    assert "did not change" in w.owner_text("Change Avery Sample's reminders to 9 AM", race).text
    assert w.saved() == (True, 6, "15:00:00")


def test_repeated_receipt_replays_original_after_later_portal_edit_and_model_failure() -> None:
    w = OwnerWorld()
    calls = commit_counter(w)
    inbound = w.inbound("Avery Sample asked for reminders 2 days before at 9 AM", "SM-owner-replay")
    w.owner_model.action = lambda t: (change(t, enabled=True, days=2, clock="09:00"), "ok")[1]
    reader = Reader({(BUSINESS, inbound.provider_id): inbound})
    processor = ReceiptProcessor(reader, w.chat, BUSINESS, lambda: RUN)
    assert "reminders on" in processor.process(BUSINESS, inbound.provider_id).text
    assert processor.process(BUSINESS, inbound.provider_id) is None
    assert len(reader.replies) == len(calls) == 1
    w.owner_edit(enabled=False, days_before=4)
    version = w.owner_view()["version"]
    w.owner_model.action = lambda _: (_ for _ in ()).throw(TimeoutError())
    # Simulate loss of reply/receipt processing after the preference transaction committed.
    result = w.chat.handle(inbound)
    assert "Original saved result" in result.text and "reminders on" in result.text
    assert "2 days before at 9:00 AM" in result.text
    assert w.saved() == (False, 4, "09:00:00") and w.owner_view()["version"] == version
    assert len(calls) == 1 and w.owner_model.calls == 1


def test_read_states_and_blocked_client_enrollment_never_changes_delivery_permissions() -> None:
    w = OwnerWorld()
    facts: list[dict[str, Any]] = []
    w.owner_text("What are Avery Sample's reminders?", lambda t: (facts.append(read(t)), "Off.")[1])
    assert not facts[-1]["enabled"] and facts[-1]["upcoming_visit_count"] == 0
    w.visit(w.target, 22)
    w.owner_text("What are Avery Sample's reminders?", lambda t: (facts.append(read(t)), "Off.")[1])
    assert facts[-1]["reminders"][0]["status"] == "disabled"
    p = w.records.read_profile(BUSINESS, w.target)
    assert p is not None
    w.records.save_profile(replace(p, version=p.version + 1, phone_verified_at=None), p.version, p.phone_e164)
    w.consent.evidence.pop(f"{BUSINESS}:{p.phone_e164}")
    w.consent.stopped.add(f"{BUSINESS}:{p.phone_e164}")
    result = w.owner_text("Avery Sample asked for reminders two days before at 9 AM",
                         lambda t: (change(t, enabled=True, days=2, clock="09:00"), "ok")[1])
    assert "Sending is currently blocked" in result.text and w.saved() == (True, 2, "09:00:00")
    w.owner_text("What are Avery Sample's reminders?", lambda t: (facts.append(read(t)), "Blocked.")[1])
    assert set(facts[-1]["blockers"]) == {"phone_unverified", "consent_missing", "sms_opted_out"}
    assert facts[-1]["reminders"][0]["status"] == "blocked"
    assert w.records.read_profile(BUSINESS, w.target).phone_verified_at is None
    assert w.consent.read_consent(BUSINESS, p.phone_e164) is None
    assert w.consent.is_opted_out(BUSINESS, p.phone_e164)
    assert w.run_delivery(datetime(2026, 10, 20, 16, 0, 15, tzinfo=UTC)) == 0
    assert w.submissions == []


def test_owner_edit_changes_only_one_clients_delivery_and_never_repeats_attempt() -> None:
    w = OwnerWorld()
    w.visit(w.target, 22)
    w.visit(w.other, 22, 20)
    w.owner_text("Blake Example asked to turn reminders on", lambda t: (
        change(t, name="Blake Example", enabled=True), "ok")[1])
    w.owner_text("Avery Sample asked for reminders two days before at 9 AM", lambda t: (
        change(t, enabled=True, days=2, clock="09:00"), "ok")[1])
    assert w.run_delivery(datetime(2026, 10, 20, 16, 0, 15, tzinfo=UTC)) == 1
    assert [item["to"] for item in w.submissions] == ["+14155550501"]
    w.owner_text("Change Avery Sample's reminder to 11 AM", lambda t: (change(t, clock="11:00"), "ok")[1])
    assert w.owner_view()["upcoming_reminders"][0]["status"] == "attempted"
    assert w.run_delivery(datetime(2026, 10, 20, 18, 0, 15, tzinfo=UTC)) == 0
    assert w.run_delivery(datetime(2026, 10, 21, 17, 0, 15, tzinfo=UTC)) == 1
    assert [item["to"] for item in w.submissions] == ["+14155550501", "+14155550502"]


def test_past_time_edit_warns_and_never_catches_up() -> None:
    w = OwnerWorld()
    w.visit(w.target, 22)
    w.owner_edit(enabled=True)
    w.clock[0] = datetime(2026, 10, 21, 19, tzinfo=UTC)
    result = w.owner_text("Change Avery Sample's reminders to 9 AM", lambda t: (
        change(t, clock="09:00"), "ok")[1])
    assert "reminder time has passed" in result.text and "skipped (no catch-up)" in result.text
    assert w.owner_view()["upcoming_reminders"][0]["status"] == "past_time"
    assert w.run_delivery(datetime(2026, 10, 21, 19, 1, 15, tzinfo=UTC)) == 0
    assert w.submissions == []


def test_model_timeout_after_commit_keeps_backend_receipt_and_blocks_other_actions() -> None:
    w = OwnerWorld()
    def action(t: Any) -> str:
        assert change(t, enabled=True)["ok"]
        assert t("approve_request", {"ref": "x", "version": 1})["error"] == "action_already_attempted"
        raise TimeoutError()
    result = w.owner_text("Avery Sample asked to turn reminders on and approve a request", action)
    assert "reminders on" in result.text and "other requests" in result.text
    assert w.saved() == (True, 1, "10:00:00")


def test_scheduling_action_first_refuses_reminder_and_reports_it_unchanged() -> None:
    w = OwnerWorld()
    HoldService(w.records).create(CreateHold(
        BUSINESS, w.target, w.target, "pending", datetime(2026, 10, 22, 17, tzinfo=UTC), 120), RUN)
    def action(t: Any) -> str:
        pending = t("list_pending_requests", {})["requests"][0]
        assert t("approve_request", {"ref": pending["ref"], "version": pending["version"]})["ok"]
        assert change(t, enabled=True)["error"] == "action_already_attempted"
        return "Approved and reminders are on."
    result = w.owner_text("Approve the request and turn on Avery Sample's reminders", action)
    assert result.committed and result.owner_reply_on_commit
    assert "Reminder settings did not change" in result.text
    assert "reminders are on" not in result.text and not w.owner_view()["enabled"]


@pytest.mark.parametrize("days,clock", [(1, "08:00"), (7, "18:00")])
def test_inclusive_ranges_and_unchanged_requests_remember_receipt_without_version_bump(
        days: int, clock: str) -> None:
    w = OwnerWorld()
    w.owner_text(f"Change Avery Sample's reminder to {days} days before at {clock}", lambda t: (
        change(t, days=days, clock=clock), "ok")[1])
    before = w.owner_view()
    calls = commit_counter(w)
    result = w.owner_text(f"Keep Avery Sample's reminder {days} days before at {clock}", lambda t: (
        change(t, days=days, clock=clock), "ok")[1], "SM-noop")
    assert "reminders off" in result.text and len(calls) == 1
    assert w.owner_view() == before
    w.owner_text("Keep Avery Sample's reminder timing", lambda _: "Should not run", "SM-noop")
    assert w.owner_view() == before and len(calls) == 1


