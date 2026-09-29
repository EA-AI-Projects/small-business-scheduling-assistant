"""The scheduled Lambda entry point constructs the AWS dispatch path."""

import json
from datetime import datetime
from typing import Any

from scheduling.workers import outbox


def test_dispatch_due_handler_uses_configured_table_and_queue(
    monkeypatch: Any, capsys: Any
) -> None:
    calls: list[tuple[Any, ...]] = []
    dynamo_client = object()
    sqs_client = object()

    def client(name: str) -> object:
        return {"dynamodb": dynamo_client, "sqs": sqs_client}[name]

    class Store:
        def __init__(self, aws_client: object, table: str) -> None:
            calls.append(("store", aws_client, table))

        def due(self, now: datetime, limit: int) -> list[tuple[str, str]]:
            return []

    class Queue:
        def __init__(self, aws_client: object, url: str) -> None:
            calls.append(("queue", aws_client, url))

    monkeypatch.setenv("SCHEDULING_TABLE_NAME", "pilot-table")
    monkeypatch.setenv("OUTBOX_QUEUE_URL", "https://sqs.example/queue")
    monkeypatch.setattr(outbox.boto3, "client", client)
    monkeypatch.setattr(outbox, "DynamoOutboxStore", Store)
    monkeypatch.setattr(outbox, "SQSIntentQueue", Queue)

    assert outbox.dispatch_due_handler({}, None) == {
        "examined": 0, "enqueued": 0, "stale": 0, "oldest_due_age_seconds": 0.0,
    }
    assert calls == [
        ("store", dynamo_client, "pilot-table"),
        ("queue", sqs_client, "https://sqs.example/queue"),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "outbox_dispatch": {
            "examined": 0,
            "enqueued": 0,
            "stale": 0,
            "oldest_due_age_seconds": 0.0,
        }
    }
