"""Disabled-by-default persisted receipt consumer."""

import json
import os
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from scheduling.adapters.conversation_state_dynamodb import DynamoConversationStates
from scheduling.adapters.counteroffer_dynamodb import DynamoCounterofferStore
from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.adapters.outbox_aws import DynamoOutboxStore, SQSIntentQueue
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.conversation import ConversationService
from scheduling.domain.holds import HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.outbox import DispatchService
from scheduling.domain.owner_counteroffer import CounterofferAcceptance, CounterofferService
from scheduling.domain.sms_ingress import InboundReceipt
from scheduling.domain.sms_processing import ReceiptProcessor, process_sqs_batch


def _openai_key(ssm: Any, name: str) -> str:
    if not name.startswith("/scheduling/"):
        raise ValueError("OpenAI key parameter must be under /scheduling/")
    parameter = ssm.get_parameter(Name=name, WithDecryption=True).get("Parameter", {})
    if parameter.get("Type") != "SecureString" or not parameter.get("Value"):
        raise ValueError("OpenAI key parameter must be a nonempty SecureString")
    return str(parameter["Value"])


def handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    if os.environ.get("SMS_CONVERSATION_ENABLED") != "authorized":
        raise RuntimeError("SMS conversation processing is not authorized")
    dynamo = boto3.client("dynamodb")
    table = os.environ["SCHEDULING_TABLE_NAME"]
    business_id = os.environ["BUSINESS_ID"]
    # Collect only successful writes, then hand off after reply drafting finishes.
    pending_outbox: dict[tuple[str, str], None] = {}

    def outbox_created(business: str, outbox_id: str) -> None:
        pending_outbox[business, outbox_id] = None

    store = DynamoSmsIngressStore(dynamo, table, outbox_created=outbox_created)
    states = DynamoConversationStates(dynamo, table)
    clock = lambda: datetime.now(UTC)
    dispatcher = DispatchService(
        DynamoOutboxStore(dynamo, table),
        SQSIntentQueue(boto3.client("sqs", config=Config(
            connect_timeout=1, read_timeout=2, retries={"total_max_attempts": 1})),
            os.environ["OUTBOX_QUEUE_URL"]), clock)

    def dispatch_saved() -> None:
        references = tuple(pending_outbox)
        pending_outbox.clear()
        dispatched = failed = 0
        for business, outbox_id in references:
            try:
                dispatched += int(dispatcher.dispatch_one(business, outbox_id))
            except Exception:  # noqa: BLE001 - saved work remains due for the sweep
                failed += 1
        # Counts only: no message content, recipient, or provider identifiers.
        print(json.dumps({"sms_immediate_dispatch": {
            "examined": len(references), "enqueued": dispatched, "failed": failed,
        }}, sort_keys=True))

    interpreter = OpenAIMessageInterpreter(_openai_key(
        boto3.client("ssm"), os.environ["OPENAI_API_KEY_PARAM"]), timeout_seconds=8)

    def conversation_for(receipt: InboundReceipt) -> ConversationService:
        # A separate repository per receipt adds STOP conditions to each
        # scheduling write, even when the batch contains different senders.
        calendar = DynamoDBCalendarRepository(
            dynamo, table, sms_sender_guard=receipt.sender,
            outbox_created=outbox_created)
        counteroffer_store = DynamoCounterofferStore(
            dynamo, table, outbox_created=outbox_created)
        return ConversationService(
            calendar, interpreter, HoldService(calendar),
            LifecycleService(calendar, clock), store, clock,
            os.environ["OWNER_NUMBER"], states,
            counteroffers=CounterofferService(
                calendar, store, counteroffer_store,
                os.environ["OWNER_NUMBER"]),
            counteroffer_acceptance=CounterofferAcceptance(
                calendar, store, counteroffer_store,
                HoldService(calendar)),
            history_reader=store,
        )

    processor = ReceiptProcessor(store, None, business_id, clock,
                                 conversation_factory=conversation_for)
    return process_sqs_batch(event["Records"], processor, after_record=dispatch_saved)
