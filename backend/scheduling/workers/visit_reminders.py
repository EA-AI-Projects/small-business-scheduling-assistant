"""Disabled-by-default selection and immediate same-minute queue handoff."""
import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.outbox_aws import DynamoOutboxStore, SQSIntentQueue
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.outbox import DispatchService
from scheduling.domain.reminders import ReminderSelector


def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    if (os.environ.get("VISIT_REMINDER_DELIVERY_ENABLED") != "authorized"
            or os.environ.get("SMS_SEND_ENABLED") != "authorized"):
        raise RuntimeError("Visit reminder selection is not authorized")
    table = os.environ["SCHEDULING_TABLE_NAME"]
    dynamo = boto3.client("dynamodb")
    def clock() -> datetime:
        return datetime.now(UTC)
    dispatch = DispatchService(DynamoOutboxStore(dynamo, table),
        SQSIntentQueue(boto3.client("sqs"), os.environ["OUTBOX_QUEUE_URL"]), clock)
    def handoff(business_id: str, outbox_id: str) -> None:
        dispatch.dispatch_one(business_id, outbox_id)
    records = DynamoDBCalendarRepository(dynamo, table, outbox_created=handoff)
    report = ReminderSelector(records, DynamoSmsIngressStore(dynamo, table)).run(
        os.environ["BUSINESS_ID"], clock())
    return asdict(report)
