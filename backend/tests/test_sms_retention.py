"""Body deletion follows the last exchange and preserves receipt metadata."""

from datetime import UTC, datetime, timedelta
from typing import Any

from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

NOW = datetime(2026, 9, 28, 17, tzinfo=UTC)


class MemoryDynamo:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self.items.get(kwargs["Key"]["SK"]["S"])
        return {"Item": item} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return {"Items": [item for key, item in self.items.items() if key.startswith("SMS#")]}

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
