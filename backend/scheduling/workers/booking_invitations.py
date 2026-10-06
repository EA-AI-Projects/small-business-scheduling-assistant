"""Disabled-by-default selection and outbox promotion; no provider access."""

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.booking_invitations import InvitationPromoter, InvitationSelector


def handler(_event: dict[str, Any], _context: object) -> dict[str, object]:
    if os.environ.get("BOOKING_INVITATION_SELECTION_ENABLED") != "authorized":
        raise RuntimeError("Booking invitation selection is not authorized")
    business_id = os.environ["BUSINESS_ID"]
    table = os.environ["SCHEDULING_TABLE_NAME"]
    dynamo = boto3.client("dynamodb")
    records = DynamoDBCalendarRepository(dynamo, table)
    consent = DynamoSmsIngressStore(dynamo, table)
    now = datetime.now(UTC)
    selected = asdict(InvitationSelector(records, consent).run(business_id, now))
    promoted: dict[str, int] = {}
    if os.environ.get("BOOKING_INVITATION_DELIVERY_ENABLED") == "authorized":
        if os.environ.get("SMS_SEND_ENABLED") != "authorized":
            raise RuntimeError("Global SMS delivery is not authorized")
        promoted = InvitationPromoter(records, consent).run(business_id, now)
    report: dict[str, object] = {"selection": selected, "delivery": promoted}
    print(json.dumps({"booking_invitations": report}, sort_keys=True))
    return report
