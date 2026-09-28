"""SQS delivery consumer; no live sending unless explicitly enabled at deployment."""

import os
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]
from twilio.rest import Client  # type: ignore[import-untyped]

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.outbox_aws import DynamoOutboxStore
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.domain.outbox import ConsumeService, consume_sqs_batch


def handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    """Provisioning the queue trigger and send authorization is a separate owner decision."""
    if os.environ.get("SMS_SEND_ENABLED") != "authorized":
        raise RuntimeError("Live SMS delivery is not authorized")
    table = os.environ["SCHEDULING_TABLE_NAME"]
    dynamo = boto3.client("dynamodb")
    sender = TwilioSmsSender(
        Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]).messages,
        DynamoDBCalendarRepository(dynamo, table), DynamoSmsIngressStore(dynamo, table),
        os.environ["BUSINESS_ID"], os.environ["TWILIO_BUSINESS_NUMBER"],
        os.environ["OWNER_NUMBER"], status_callback=os.environ.get("TWILIO_STATUS_URL"),
        authorized_recipients=frozenset(
            number.strip() for number in os.environ["AUTHORIZED_SMS_RECIPIENTS"].split(",")
            if number.strip()
        ),
    )
    consumer = ConsumeService(DynamoOutboxStore(dynamo, table), sender,
                              lambda: datetime.now(UTC))
    return consume_sqs_batch(event["Records"], consumer)
