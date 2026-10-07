"""Conversation history at the persisted SMS record boundary."""

from datetime import UTC, datetime, timedelta
from typing import Any

from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.conversation_history import MAX_CHARACTERS, MAX_MESSAGES
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

NOW = datetime(2026, 10, 14, 0, 10, tzinfo=UTC)
PHONE = "+15005550006"


class Records:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        return {"Items": [item for item in self.items
                          if item["PK"]["S"] == values[":pk"]["S"]
                          and item["SK"]["S"].startswith(values[":prefix"]["S"])]}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        return {"Item": next((item for item in self.items
                              if item["PK"] == key["PK"] and item["SK"] == key["SK"]), None)}


def inbound(provider_id: str, body: str, when: datetime, *,
            phone: str = PHONE, client_id: str = "client-1",
            business: str = "pilot") -> dict[str, Any]:
    return {"PK": {"S": f"BUSINESS#{business}"}, "SK": {"S": f"SMS#{provider_id}"},
            "provider_id": {"S": provider_id}, "sender": {"S": phone},
            "recipient": {"S": "+15005550000"}, "keyword": {"S": "OTHER"},
            "authorized_for_commands": {"BOOL": True},
            "role": {"S": "client"}, "client_id": {"S": client_id},
            "received_at": {"S": when.isoformat()}, "body": {"S": body}}


def sent(provider_id: str, body: str, when: datetime, *, template: str = "conversation-reply",
         client_id: str = "client-1", business: str = "pilot") -> dict[str, Any]:
    return {"PK": {"S": f"BUSINESS#{business}"}, "SK": {"S": f"SMS_OUT#{provider_id}"},
            "provider_id": {"S": provider_id}, "recipient": {"S": PHONE},
            "client_id": {"S": client_id}, "sent_at": {"S": when.isoformat()},
            "template": {"S": template}, "body": {"S": body}}


def test_invitation_two_replies_failed_send_and_cross_midnight() -> None:
    records = Records()
    records.items = [
        sent("invite", "Want to book?", NOW - timedelta(minutes=20),
             template="booking_invitation"),
        inbound("first", "Oct 13 at 1 pm", NOW - timedelta(minutes=15)),
        sent("answer", "That time is available.", NOW - timedelta(minutes=14)),
        inbound("second", "Oct 13", NOW - timedelta(minutes=5)),
        # A failed outbox draft has no SMS_OUT handoff evidence.
        {"PK": {"S": "BUSINESS#pilot"}, "SK": {"S": "OUTBOX#failed"},
         "body": {"S": "This was never sent"}},
        inbound("other", "Another client", NOW - timedelta(minutes=4), client_id="client-2"),
        sent("other-out", "Private reply", NOW - timedelta(minutes=3), client_id="client-2"),
        inbound("business", "Wrong business", NOW - timedelta(minutes=2), business="other"),
        inbound("old", "Outside window", NOW - timedelta(hours=25)),
    ]
    receipt = InboundReceipt("pilot", "second", PHONE, "+15005550000", "Oct 13",
                             NOW - timedelta(minutes=5), SenderRole.CLIENT,
                             "client-1", Keyword.OTHER, True)
    history = DynamoSmsIngressStore(records, "table").read_conversation_history(receipt, NOW)
    assert [(item.role, item.text) for item in history] == [
        ("assistant", "Want to book?"), ("client", "Oct 13 at 1 pm"),
        ("assistant", "That time is available."), ("client", "Oct 13"),
    ]
    assert history[0].at.date() != history[-1].at.date()


def test_bounds_keep_latest_and_invitation_without_shortening_text() -> None:
    records = Records()
    records.items = [sent("invite", "Invitation", NOW - timedelta(hours=2),
                          template="booking_invitation")]
    records.items += [inbound(f"in-{i:02}", "x" * 400,
                              NOW - timedelta(minutes=80 - i)) for i in range(80)]
    receipt = InboundReceipt("pilot", "in-79", PHONE, "+15005550000", "x" * 400,
                             NOW - timedelta(minutes=1), SenderRole.CLIENT,
                             "client-1", Keyword.OTHER, True)
    history = DynamoSmsIngressStore(records, "table").read_conversation_history(receipt, NOW)
    assert len(history) <= MAX_MESSAGES
    assert sum(len(item.text) for item in history) <= MAX_CHARACTERS
    assert history[0].text == "Invitation"
    assert history[-1].provider_id == "in-79"
    assert all(item.text == "Invitation" or item.text == "x" * 400 for item in history)
