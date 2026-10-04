"""Scheduled SMS body and four-year evidence retention for the pilot."""

import os
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.sms_ingress import normalize_phone


def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    store = DynamoSmsIngressStore(boto3.client("dynamodb"), os.environ["SCHEDULING_TABLE_NAME"])
    # Fail closed: an empty or malformed owner number must not fall back to the client rule.
    owner_number = normalize_phone(os.environ["OWNER_NUMBER"].strip())
    now = datetime.now(UTC)
    business_id = os.environ["BUSINESS_ID"]
    bodies = store.purge_expired_bodies(business_id, now, owner_number)
    evidence = store.purge_expired_evidence(business_id, now)
    return {"deleted_sms_bodies": bodies, "deleted_sms_evidence": evidence}
