"""Scheduled outbox dispatcher; infrastructure binds the schedule and queue."""

import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.outbox_aws import DynamoOutboxStore, SQSIntentQueue
from scheduling.domain.outbox import DispatchService


def dispatch_due_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """EventBridge Lambda entry point for the provider-independent SQS handoff."""
    store = DynamoOutboxStore(
        boto3.client("dynamodb"), os.environ["SCHEDULING_TABLE_NAME"]
    )
    queue = SQSIntentQueue(boto3.client("sqs"), os.environ["OUTBOX_QUEUE_URL"])
    return asdict(DispatchService(store, queue, lambda: datetime.now(UTC)).run_once())
