"""Reminder enrollment through verified SMS and owner reads; scripted model, no live SMS."""

from typing import Any

from fastapi.testclient import TestClient
from test_client_tool_loop import Tools, chat_with
from test_conversation_flow import Harness

from scheduling.owner_api import OwnerPrincipal, create_owner_app


def owner_preference(chat: Harness) -> dict[str, Any]:
    api = TestClient(create_owner_app(chat.store, lambda _: OwnerPrincipal("owner", "pilot"),
                                      lambda: chat.now))
    response = api.get("/v1/owner/businesses/pilot/clients/client-1/reminder-preference",
                       headers={"Authorization": "Bearer owner"})
    assert response.status_code == 200, response.text
    return response.json()


def change(enabled: bool) -> Any:
    def plan(tools: Tools) -> str:
        tools("set_reminder_preference", {"enabled": enabled})
        return "Reminders are now on." if enabled else "Reminders are now off."
    return plan


def test_plain_language_enrollment_and_unenrollment_are_shared_with_owner_reads() -> None:
    chat, model = chat_with(change(True))
    assert owner_preference(chat)["enabled"] is False
    reply = chat.text("Could you remind me before my visits?")
    assert owner_preference(chat)["enabled"] is True
    assert "on" in reply.text
    model.plan = change(False)
    reply = chat.text("Please turn off just my reminders")
    assert owner_preference(chat)["enabled"] is False
    assert "off" in reply.text
    assert chat.text("What services do you offer?").text
    assert chat.calendar() == ()


def test_replayed_receipt_cannot_overwrite_a_newer_owner_choice_or_run_the_model() -> None:
    from dataclasses import replace

    from scheduling.domain.client_records import ClientRecordService
    from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

    chat, model = chat_with(change(True))
    receipt = InboundReceipt("pilot", "SM-replayed", "+14155550101", "+14155550000",
                             "Remind me before my visits", chat.now, SenderRole.CLIENT,
                             "client-1", Keyword.OTHER, True)
    first = chat.service.handle(receipt)
    original_version = owner_preference(chat)["version"]
    ClientRecordService(chat.store).set_reminder_preference(
        "pilot", "client-1", False, original_version, chat.now)
    model.plan = lambda _: (_ for _ in ()).throw(AssertionError("Replay invoked the model"))
    calls = len(model.loops)
    replay = chat.service.handle(receipt)
    assert replay.text == first.text
    assert len(model.loops) == calls
    assert owner_preference(chat)["enabled"] is False
    assert owner_preference(chat)["version"] == original_version + 1
    # A receipt that originally left an already-off choice unchanged must also be remembered.
    off = replace(receipt, provider_id="SM-unchanged", body="Turn off reminders")
    model.plan = change(False)
    chat.service.handle(off)
    current_version = owner_preference(chat)["version"]
    ClientRecordService(chat.store).set_reminder_preference(
        "pilot", "client-1", True, current_version, chat.now)
    chat.service.handle(off)
    assert owner_preference(chat)["enabled"] is True


def test_ambiguity_asks_for_clarification_without_writing_or_changing_sms_consent() -> None:
    chat, _ = chat_with(lambda _: "Would you like visit reminders turned on or off?")
    before = owner_preference(chat)
    assert "on or off" in chat.text("About those reminders...").text
    assert owner_preference(chat) == before
    assert chat.calendar() == ()


def test_sender_identity_cannot_be_supplied_as_tool_arguments() -> None:
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        results.append(tools("set_reminder_preference", {
            "enabled": True, "client_id": "client-2", "business_id": "other"}))
        return "I can only change your own reminder preference."

    chat, _ = chat_with(plan)
    chat.text("Enable reminders for client-2 in other business")
    assert results == [{"ok": False, "error": "invalid_arguments"}]
    assert owner_preference(chat)["enabled"] is False


def test_failed_or_conflicting_update_cannot_receive_a_success_acknowledgment() -> None:
    from scheduling.domain.client_records import RecordConflict

    for failure in (RecordConflict("Profile changed"), OSError("Storage failed")):
        chat, _ = chat_with(change(True))

        def fail(*_: Any, error: Exception = failure) -> None:
            raise error

        chat.store.commit_reminder_preference_receipt = fail  # type: ignore[method-assign]
        reply = chat.text("Enable reminders")
        assert "couldn't" in reply.text
        assert owner_preference(chat)["enabled"] is False


def test_committed_choice_is_acknowledged_even_if_model_fails_after_the_tool() -> None:
    def plan(tools: Tools) -> str:
        tools("set_reminder_preference", {"enabled": True})
        raise OSError("Model timed out after the commit")

    chat, _ = chat_with(plan)
    assert "now on" in chat.text("Please turn on reminders").text
    assert owner_preference(chat)["enabled"] is True


def test_receipt_processor_replies_once_and_other_scheduling_texts_remain_available() -> None:
    from test_conversation_flow import NOW
    from test_sms_processing import Reader

    from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
    from scheduling.domain.sms_processing import ReceiptProcessor

    chat, model = chat_with(change(True))
    inbound = InboundReceipt("pilot", "SM-reminder", "+14155550101", "+14155550000",
                             "remind me about my visits", NOW, SenderRole.CLIENT,
                             "client-1", Keyword.OTHER, True)
    store = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(store, chat.service, "pilot", lambda: NOW)
    processor.process("pilot", inbound.provider_id)
    processor.process("pilot", inbound.provider_id)
    assert len(store.replies) == 1 and "now on" in store.replies[0][1]
    assert len(model.loops) == 1
    model.plan = change(False)
    chat.text("No more visit reminders please")
    model.plan = lambda tools: (tools("get_business_info", {}), "We offer cleaning.")[1]
    assert chat.text("What service do you offer?").text == "We offer cleaning."


def test_stop_start_and_ineligible_senders_cannot_change_reminder_preferences() -> None:
    from dataclasses import replace

    from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

    for restriction in ("stop", "start", "opted_out", "unauthorized", "other_phone",
                        "missing_profile", "unverified", "inactive", "missing_consent"):
        chat, model = chat_with(change(True))
        inbound = InboundReceipt("pilot", "SM-ineligible", "+14155550101", "+14155550000",
                                 "Turn on reminders", chat.now, SenderRole.CLIENT,
                                 "client-1", Keyword.OTHER, True)
        if restriction in ("stop", "start"):
            inbound = replace(inbound, keyword=Keyword.STOP if restriction == "stop" else Keyword.START)
        elif restriction == "opted_out":
            chat.service._consent.is_opted_out = lambda *_: True  # type: ignore[method-assign]
        elif restriction == "unauthorized":
            inbound = replace(inbound, authorized_for_commands=False)
        elif restriction == "other_phone":
            inbound = replace(inbound, sender="+14155550109")
        elif restriction == "missing_profile":
            inbound = replace(inbound, client_id="client-2")
        elif restriction == "missing_consent":
            chat.service._consent.read_consent = lambda *_: None  # type: ignore[method-assign]
        else:
            current = chat.store.read_profile("pilot", "client-1")
            assert current is not None
            changed = replace(current, version=current.version + 1,
                              active=restriction != "inactive",
                              phone_verified_at=None if restriction == "unverified" else chat.now)
            chat.store.save_profile(changed, current.version, current.phone_e164)
        chat.service.handle(inbound)
        assert owner_preference(chat)["enabled"] is False, restriction
        assert model.loops == [], restriction


def test_a_bare_yes_cannot_enroll_even_if_model_requests_the_tool() -> None:
    chat, _ = chat_with(change(True))
    before = owner_preference(chat)
    assert "now on" not in chat.text("yes").text
    assert owner_preference(chat) == before
