"""Public signed Twilio webhook Lambda; no conversation or SMS send occurs here."""

import os

import boto3  # type: ignore[import-untyped]
from mangum import Mangum

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.adapters.sms_queue import SQSReceiptQueue
from scheduling.adapters.twilio_secret import load_twilio_auth_token
from scheduling.domain.sms_ingress import SmsIngressService
from scheduling.twilio_webhooks import create_twilio_ingress_app

_dynamodb = boto3.client("dynamodb")
_table = os.environ["SCHEDULING_TABLE_NAME"]
_business_id = os.environ["BUSINESS_ID"]
_store = DynamoSmsIngressStore(_dynamodb, _table)
_service = SmsIngressService(
    _store,
    DynamoDBCalendarRepository(_dynamodb, _table),
    _business_id,
    os.environ["TWILIO_BUSINESS_NUMBER"],
    os.environ["OWNER_NUMBER"],
)
_token = load_twilio_auth_token(
    boto3.client("ssm"), os.environ["TWILIO_AUTH_TOKEN_PARAM"]
)
app = create_twilio_ingress_app(
    _service,
    _token,
    os.environ["TWILIO_INBOUND_URL"],
    status_url=os.environ["TWILIO_STATUS_URL"],
    status_store=_store,
    business_id=_business_id,
    receipt_queue=(SQSReceiptQueue(
        boto3.client("sqs"), os.environ["SMS_CONVERSATION_QUEUE_URL"])
        if os.environ.get("SMS_CONVERSATION_HANDOFF_ENABLED") == "authorized" else None),
)
handler = Mangum(app, lifespan="off")
