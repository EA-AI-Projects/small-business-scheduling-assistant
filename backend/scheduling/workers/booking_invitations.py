"""Dormant invitation selector entrypoint; no SAM schedule or SMS route is wired."""

import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.booking_invitations import InvitationSelector


def handler(_event: dict[str, Any], _context: object) -> dict[str, object]:
    if os.environ.get("BOOKING_INVITATION_SELECTION_ENABLED") != "authorized":
        raise RuntimeError("Booking invitation selection is not authorized")
    business_id = os.environ["BUSINESS_ID"]
    table = os.environ["SCHEDULING_TABLE_NAME"]
    dynamo = boto3.client("dynamodb")
    selector = InvitationSelector(
        DynamoDBCalendarRepository(dynamo, table), DynamoSmsIngressStore(dynamo, table)
    )
    return asdict(selector.run(business_id, datetime.now(UTC)))
