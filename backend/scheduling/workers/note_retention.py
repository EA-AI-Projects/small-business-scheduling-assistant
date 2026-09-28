"""Scheduled deletion of expired ordinary notes for the pilot business."""

import os
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.domain.client_records import ClientRecordRepository, ClientRecordService


def purge_business_notes(repository: ClientRecordRepository,
                         business_id: str, now: datetime) -> int:
    if not business_id:
        raise ValueError("Business ID is required")
    service = ClientRecordService(repository)
    return sum(service.purge_expired_notes(business_id, profile.client_id, now)
               for profile in repository.list_profiles(business_id))


def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    """Deploy a periodic trigger only with the separately reviewed pilot stack."""
    repository = DynamoDBCalendarRepository(
        boto3.client("dynamodb"), os.environ["SCHEDULING_TABLE_NAME"])
    count = purge_business_notes(repository, os.environ["BUSINESS_ID"], datetime.now(UTC))
    return {"deleted_notes": count}
