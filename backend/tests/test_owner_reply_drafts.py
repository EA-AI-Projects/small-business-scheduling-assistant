"""The #299 owner calendar reply is the tool loop's final SMS.

There is no second owner drafter or backend fact rewrite in this path.
"""

import pytest
from test_owner_calendar_questions import Chat, local

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.conversation import OWNER_FAILURE_TEXT

ASK = "What is on Thursday?"


def world() -> tuple[Chat, str]:
    chat = Chat()
    request = chat.hold("c1", local(10, 1, 9), "a")
    chat.query(ASK, "2026-10-01", "2026-10-01", ["pending"])
    return chat, request


def test_model_final_calendar_sms_is_sent_whole_without_second_draft() -> None:
    chat, request = world()
    message = f"Avery has a pending request Thursday at 9 AM (ref {request[:8]})."
    chat.model.final_text[ASK] = message
    answer = chat.ask(ASK)
    assert answer.text == message and not answer.committed
    assert chat.model.calls == [ASK] and len(chat.model.results) == 1
    assert chat.model.results[0]["entries"][0]["ref"] == request[:8]
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_model_final_calendar_sms_is_not_fact_checked() -> None:
    chat, request = world()
    chat.model.final_text[ASK] = "The visit is Friday at 3 PM."
    answer = chat.ask(ASK)
    assert answer.text == "The visit is Friday at 3 PM."
    assert not answer.committed and chat.status(request) == CalendarStatus.PENDING_APPROVAL


@pytest.mark.parametrize("invalid", ["", "Approved 😀", "A" * 481])
def test_undeliverable_final_calendar_sms_uses_fixed_failure(invalid: str) -> None:
    chat, request = world()
    chat.model.final_text[ASK] = invalid
    answer = chat.ask(ASK)
    assert answer.text == OWNER_FAILURE_TEXT and not answer.committed
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_calendar_followup_receives_the_owner_transcript() -> None:
    chat, request = world()
    chat.ask(ASK)
    chat.query("And Friday?", "2026-10-02", "2026-10-02", ["pending"])
    chat.ask("And Friday?")
    history = chat.model.histories[-1]
    assert any(message.role == "owner" and message.text == ASK for message in history)
    assert any(message.role == "assistant" and "Calendar:" in message.text
               for message in history)
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
