"""EventBridge entry point for pending-hold expiry."""

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.domain.expiry import ExpiryService
from scheduling.domain.lifecycle import LifecycleService


def expire_due_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Expire one bounded page; a later invocation sees any remaining due holds."""
    repository = DynamoDBCalendarRepository(
        boto3.client("dynamodb"), os.environ["SCHEDULING_TABLE_NAME"]
    )
    clock = lambda: datetime.now(UTC)
    report = ExpiryService(repository, LifecycleService(repository, clock), clock).run_once()
    result = asdict(report)
    print(json.dumps({"hold_expiry": result}, sort_keys=True))
    return result
