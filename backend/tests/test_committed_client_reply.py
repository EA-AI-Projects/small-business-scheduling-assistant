"""A committed client draft can only attach to the original pending intent."""

from datetime import UTC, datetime
from typing import Any

from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole


class Client:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.writes.append(kwargs)
        return {}


def test_draft_attach_is_conditional_on_pending_original_outbox() -> None:
    client = Client()
    store = DynamoSmsIngressStore(client, "synthetic-table")  # type: ignore[arg-type]
    receipt = InboundReceipt("pilot", "SM-in", "+14155550101", "+14155550000",
                             "YES", datetime(2026, 10, 12, tzinfo=UTC),
                             SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    assert store.put_committed_reply(receipt, "hold-1#client",
                                     "Pending owner approval. Ref a101a101.", "lease")
    writes = client.writes[0]["TransactItems"]
    assert len(writes) == 3
    receipt_write, intent_write, erasure_check = writes
    assert receipt_write["Update"]["Key"]["SK"]["S"] == "SMS#SM-in"
    assert intent_write["Update"]["Key"]["SK"]["S"] == "OUTBOX#hold-1#client"
    condition = intent_write["Update"]["ConditionExpression"]
    assert "delivery_state = :pending" in condition
    assert "attribute_not_exists(reply_provider_id)" in condition
    assert erasure_check["ConditionCheck"]["Key"]["SK"]["S"].startswith("ERASURE#")
    assert not store.put_committed_reply(receipt, "hold-1#client", "x" * 161, "lease")
    assert len(client.writes) == 1
