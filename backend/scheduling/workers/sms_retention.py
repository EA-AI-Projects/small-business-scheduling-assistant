"""Scheduled SMS body and four-year evidence retention for the pilot."""

import os
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore


def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    store = DynamoSmsIngressStore(boto3.client("dynamodb"), os.environ["SCHEDULING_TABLE_NAME"])
    now = datetime.now(UTC)
    business_id = os.environ["BUSINESS_ID"]
    bodies = store.purge_expired_bodies(business_id, now)
    evidence = store.purge_expired_evidence(business_id, now)
    return {"deleted_sms_bodies": bodies, "deleted_sms_evidence": evidence}
