"""Body deletion follows the last exchange and preserves receipt metadata."""

from datetime import UTC, datetime, timedelta
from typing import Any

from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_status import SmsDeliveryStatus

NOW = datetime(2026, 9, 28, 17, tzinfo=UTC)


class MemoryDynamo:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self.items.get(kwargs["Key"]["SK"]["S"])
        return {"Item": item} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]["S"]
        return {"Items": [item for key, item in self.items.items() if key.startswith(prefix)]}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        for action in kwargs["TransactItems"]:
            if "Put" in action:
                item = action["Put"]["Item"]
                key = item["SK"]["S"]
                if key in self.items:
                    raise RuntimeError("duplicate receipt")
                self.items[key] = dict(item)
            elif "ConditionCheck" in action:
                check = action["ConditionCheck"]
                key = check["Key"]["SK"]["S"]
                assert self.items[key]["last_exchange_at"]["S"] <= (
                    check["ExpressionAttributeValues"][":cutoff"]["S"]
                )
            else:
                update = action["Update"]
                key = update["Key"]["SK"]["S"]
                if update["UpdateExpression"] == "REMOVE body":
                    self.items[key].pop("body")
                else:
                    self.items.setdefault(key, {"SK": {"S": key}})["last_exchange_at"] = (
                        update["ExpressionAttributeValues"][":at"]
                    )
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]["SK"]["S"]
        values = kwargs["ExpressionAttributeValues"]
        previous = self.items.get(key)
        rank = int(values[":rank"]["N"])
        if previous is not None and int(previous["status_rank"]["N"]) > rank:
            return {}
        self.items[key] = {
            "SK": {"S": key}, "status_rank": values[":rank"],
            "delivery_status": values[":status"], "recipient": values[":recipient"],
            "observed_at": values[":at"], "outbox_id": values[":outbox"],
            "provider_id": values[":provider"],
        }
        if ":error" in values:
            self.items[key]["error_code"] = values[":error"]
        return {}


def _receipt(sid: str, when: datetime) -> InboundReceipt:
    return InboundReceipt("pilot", sid, "+14155550101", "+14155550000", "Next Tuesday",
                          when, SenderRole.CLIENT, "client-1", Keyword.OTHER, True)


def test_body_waits_until_90_days_after_last_exchange() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    first = NOW - timedelta(days=100)
    latest = NOW - timedelta(days=20)
    assert store.put_received(_receipt("SM-first", first)) is True
    assert store.put_received(_receipt("SM-last", latest)) is True
    assert store.purge_expired_bodies("pilot", NOW) == 0
    assert dynamo.items["SMS#SM-first"]["body"]["S"] == "Next Tuesday"
    assert store.purge_expired_bodies("pilot", latest + timedelta(days=90)) == 2
    assert "body" not in dynamo.items["SMS#SM-first"]
    assert "provider_id" in dynamo.items["SMS#SM-first"]
    assert store.put_received(_receipt("SM-first", first)) is False


def test_outbound_reply_extends_last_exchange_window() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    first = NOW - timedelta(days=100)
    outbound = NOW - timedelta(days=20)
    store.put_received(_receipt("SM-first", first))
    store.record_outbound("pilot", "+14155550101", "SM-out", outbound)
    assert store.purge_expired_bodies("pilot", NOW) == 0
    assert store.purge_expired_bodies("pilot", outbound + timedelta(days=90)) == 1
    assert "body" not in dynamo.items["SMS#SM-first"]
    assert dynamo.items["SMS_OUT#SM-out"]["recipient"]["S"] == "+14155550101"


def test_failed_provider_callback_is_linked_for_owner_follow_up() -> None:
    store = DynamoSmsIngressStore(MemoryDynamo(), "synthetic")
    store.put_status(SmsDeliveryStatus("pilot", "outbox-1", "SM-failed",
                                       "undelivered", "+14155550101", NOW, "30007"))
    failures = store.list_delivery_failures("pilot")
    assert len(failures) == 1
    assert failures[0].outbox_id == "outbox-1"
    assert failures[0].error_code == "30007"
