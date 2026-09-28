"""Scheduled SMS body purge for the owner-approved 90-day pilot period."""

import os
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore


def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    store = DynamoSmsIngressStore(boto3.client("dynamodb"), os.environ["SCHEDULING_TABLE_NAME"])
    count = store.purge_expired_bodies(os.environ["BUSINESS_ID"], datetime.now(UTC))
    return {"deleted_sms_bodies": count}
