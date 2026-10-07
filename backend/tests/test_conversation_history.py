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
        self.pages = 0
        self.limits: list[int] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.pages += 1
        self.limits.append(kwargs["Limit"])
        values = kwargs["ExpressionAttributeValues"]
        rows = [item for item in self.items
                if item["PK"]["S"] == values[":pk"]["S"]
                and values[":from"]["S"] <= item["SK"]["S"]
                <= values[":through"]["S"]]
        rows.sort(key=lambda item: item["SK"]["S"], reverse=True)
        start = kwargs.get("ExclusiveStartKey")
        if start:
            rows = rows[next(index + 1 for index, item in enumerate(rows)
                             if item["SK"] == start["SK"]):]
        page = rows[:min(3, kwargs["Limit"])]
        return {"Items": page, **({"LastEvaluatedKey": {"PK": page[-1]["PK"],
                                                  "SK": page[-1]["SK"]}}
                                  if len(rows) > len(page) else {})}

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


def index_records(records: Records) -> None:
    for item in list(records.items):
        sk = item["SK"]["S"]
        if not sk.startswith(("SMS#", "SMS_OUT#")):
            continue
        outbound = sk.startswith("SMS_OUT#")
        phone = item["recipient" if outbound else "sender"]["S"]
        role = "client" if "client_id" in item else "owner"
        actor_id = item["client_id"]["S"] if role == "client" else phone
        at = item["sent_at" if outbound else "received_at"]["S"]
        records.items.append({"PK": item["PK"], "SK": {"S":
            DynamoSmsIngressStore._history_key(role, actor_id, at, item["provider_id"]["S"])},
            "record_sk": {"S": sk},
            "direction": {"S": "outbound" if outbound else "inbound"}})
        if outbound and item.get("template", {}).get("S") == "booking_invitation":
            records.items.append({"PK": item["PK"], "SK": {"S":
                DynamoSmsIngressStore._invitation_key(actor_id, at, item["provider_id"]["S"])},
                "record_sk": {"S": sk}})


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
    index_records(records)
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
    index_records(records)
    history = DynamoSmsIngressStore(records, "table").read_conversation_history(receipt, NOW)
    assert len(history) <= MAX_MESSAGES
    assert sum(len(item.text) for item in history) <= MAX_CHARACTERS
    assert history[0].text == "Invitation"
    assert history[-1].provider_id == "in-79"
    assert all(item.text == "Invitation" or item.text == "x" * 400 for item in history)
    assert records.pages > 1


def test_large_unrelated_history_never_enters_actor_range() -> None:
    records = Records()
    records.items = [inbound("current", "Current", NOW - timedelta(minutes=1))]
    records.items += [inbound(f"other-{i}", "Unrelated", NOW - timedelta(minutes=2),
                              phone="+15005550007", client_id="other") for i in range(1000)]
    index_records(records)
    receipt = InboundReceipt("pilot", "current", PHONE, "+15005550000", "Current",
                             NOW - timedelta(minutes=1), SenderRole.CLIENT,
                             "client-1", Keyword.OTHER, True)
    history = DynamoSmsIngressStore(records, "table").read_conversation_history(receipt, NOW)
    assert [message.text for message in history] == ["Current"]
    assert records.pages == 2  # One bounded actor page and one invitation lookup.
    assert records.limits == [64, 1]


def test_actor_burst_stops_after_fixed_pointer_budget() -> None:
    records = Records()
    records.items = [inbound(f"in-{i:03}", f"Text {i}",
                             NOW - timedelta(minutes=100 - i)) for i in range(100)]
    index_records(records)
    receipt = InboundReceipt("pilot", "in-099", PHONE, "+15005550000", "Text 99",
                             NOW - timedelta(minutes=1), SenderRole.CLIENT,
                             "client-1", Keyword.OTHER, True)
    history = DynamoSmsIngressStore(records, "table").read_conversation_history(receipt, NOW)
    assert history[-1].text == "Text 99"
    assert all(message.text != "Text 0" for message in history)
    assert records.pages == 23  # 64 pointers in pages of three, then one invitation query.
    assert records.limits[-2:] == [1, 1]


def test_phone_reassignment_does_not_hide_prior_client_invitation() -> None:
    records = Records()
    records.items = [
        sent("old-invite", "Old client's invitation", NOW - timedelta(hours=2),
             template="booking_invitation", client_id="client-1"),
        inbound("old-reply", "Old client's reply", NOW - timedelta(minutes=90)),
        sent("new-invite", "New client's invitation", NOW - timedelta(minutes=80),
             template="booking_invitation", client_id="client-2"),
    ]
    records.items += [inbound(f"new-{i}", f"New text {i}",
                              NOW - timedelta(minutes=79 - i), client_id="client-2")
                      for i in range(70)]
    index_records(records)
    receipt = InboundReceipt("pilot", "old-reply", PHONE, "+15005550000",
                             "Old client's reply", NOW - timedelta(minutes=90),
                             SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    history = DynamoSmsIngressStore(records, "table").read_conversation_history(receipt, NOW)
    assert [message.text for message in history] == [
        "Old client's invitation", "Old client's reply"]
