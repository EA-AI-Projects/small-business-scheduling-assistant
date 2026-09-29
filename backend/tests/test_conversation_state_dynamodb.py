"""Durable SMS offer memory round-trips, expires, and never erases a newer prompt."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scheduling.adapters.conversation_state_dynamodb import DynamoConversationStates
from scheduling.domain.conversation_state import PROMPT_LIFETIME, ConversationState, PromptKind

NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)


class ConditionFailed(Exception):
    def __init__(self) -> None:
        super().__init__("condition failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.reads: list[dict[str, Any]] = []

    @staticmethod
    def _key(key: dict[str, Any]) -> tuple[str, str]:
        return key["PK"]["S"], key["SK"]["S"]

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(kwargs)
        item = self.items.get(self._key(kwargs["Key"]))
        return {"Item": item} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.items[self._key(kwargs["Item"])] = kwargs["Item"]
        return {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        key = self._key(kwargs["Key"])
        current = self.items.get(key)
        expected = kwargs["ExpressionAttributeValues"][":id"]["S"]
        if current is None or current["state_id"]["S"] != expected:
            raise ConditionFailed()
        del self.items[key]
        return {}


def offer(state_id: str = "SM-1") -> ConversationState:
    return ConversationState(
        "pilot", "+14155550101", state_id, PromptKind.OFFER, NOW, NOW + PROMPT_LIFETIME,
        (NOW + timedelta(days=1), NOW + timedelta(days=1, hours=2)), "appt-1", 3)


def test_state_round_trips_with_a_ttl_and_no_message_text() -> None:
    table = FakeTable()
    states = DynamoConversationStates(table, "scheduling-test")
    states.put_state(offer())
    item = table.items[("BUSINESS#pilot", "SMS_STATE#+14155550101")]
    assert item["expires_at_epoch"]["N"] == str(int((NOW + PROMPT_LIFETIME).timestamp()))
    assert not {"body", "reply_text", "text"} & set(item)
    assert states.read_state("pilot", "+14155550101") == offer()
    assert table.reads[-1]["ConsistentRead"] is True
    assert states.read_state("pilot", "+14155550102") is None


def test_confirmation_prompt_without_options_round_trips() -> None:
    table = FakeTable()
    states = DynamoConversationStates(table, "scheduling-test")
    prompt = ConversationState("pilot", "+14155550101", "SM-2", PromptKind.CONFIRM_CANCEL,
                               NOW, NOW + PROMPT_LIFETIME, (), "appt-1", 2)
    states.put_state(prompt)
    assert "options" not in table.items[("BUSINESS#pilot", "SMS_STATE#+14155550101")]
    assert states.read_state("pilot", "+14155550101") == prompt


def test_clear_only_removes_the_prompt_it_answered() -> None:
    table = FakeTable()
    states = DynamoConversationStates(table, "scheduling-test")
    states.put_state(offer("SM-1"))
    states.put_state(offer("SM-2"))
    states.clear_state("pilot", "+14155550101", "SM-1")  # Stale clear is ignored.
    current = states.read_state("pilot", "+14155550101")
    assert current is not None and current.state_id == "SM-2"
    states.clear_state("pilot", "+14155550101", "SM-2")
    assert states.read_state("pilot", "+14155550101") is None
    states.clear_state("pilot", "+14155550101", "SM-2")  # Already gone is fine.


def test_other_delete_failures_are_not_hidden() -> None:
    class Broken(FakeTable):
        def delete_item(self, **kwargs: Any) -> dict[str, Any]:
            raise OSError("synthetic network failure")

    with pytest.raises(OSError):
        DynamoConversationStates(Broken(), "scheduling-test").clear_state(
            "pilot", "+14155550101", "SM-1")


def test_expired_item_is_still_read_as_expired_before_ttl_removes_it() -> None:
    table = FakeTable()
    states = DynamoConversationStates(table, "scheduling-test")
    states.put_state(offer())
    current = states.read_state("pilot", "+14155550101")
    assert current is not None
    assert not current.expired(NOW + PROMPT_LIFETIME - timedelta(seconds=1))
    assert current.expired(NOW + PROMPT_LIFETIME)


def test_state_rejects_naive_times_and_malformed_prompts() -> None:
    with pytest.raises(ValueError):
        ConversationState("pilot", "+1", "SM", PromptKind.OFFER, NOW.replace(tzinfo=None),
                          NOW + PROMPT_LIFETIME, (NOW,))
    with pytest.raises(ValueError):
        ConversationState("pilot", "+1", "SM", PromptKind.OFFER, NOW, NOW + PROMPT_LIFETIME, ())
    with pytest.raises(ValueError):
        ConversationState("pilot", "+1", "SM", PromptKind.CONFIRM_CANCEL, NOW,
                          NOW + PROMPT_LIFETIME, (NOW,), "appt-1")


def test_list_of_visits_round_trips_without_a_single_appointment() -> None:
    table = FakeTable()
    states = DynamoConversationStates(table, "scheduling-test")
    prompt = ConversationState("pilot", "+14155550101", "SM-3", PromptKind.CHOOSE_CANCEL,
                               NOW, NOW + PROMPT_LIFETIME, (NOW + timedelta(days=1),))
    states.put_state(prompt)
    assert states.read_state("pilot", "+14155550101") == prompt
    with pytest.raises(ValueError):
        ConversationState("pilot", "+1", "SM", PromptKind.CHOOSE_MOVE, NOW,
                          NOW + PROMPT_LIFETIME, (NOW,), "appt-1")
