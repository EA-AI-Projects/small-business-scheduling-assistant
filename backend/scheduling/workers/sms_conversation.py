"""Disabled-by-default persisted receipt consumer."""

import os
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.conversation_state_dynamodb import DynamoConversationStates
from scheduling.adapters.counteroffer_dynamodb import DynamoCounterofferStore
from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.adapters.owner_question_dynamodb import DynamoQuestionContexts
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.conversation import ConversationService
from scheduling.domain.holds import HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.owner_calendar_questions import OwnerCalendarQuestions
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
    store = DynamoSmsIngressStore(dynamo, table)
    states = DynamoConversationStates(dynamo, table)
    clock = lambda: datetime.now(UTC)
    interpreter = OpenAIMessageInterpreter(_openai_key(
        boto3.client("ssm"), os.environ["OPENAI_API_KEY_PARAM"]), timeout_seconds=8)

    def conversation_for(receipt: InboundReceipt) -> ConversationService:
        # A separate repository per receipt adds STOP conditions to each
        # scheduling write, even when the batch contains different senders.
        calendar = DynamoDBCalendarRepository(
            dynamo, table, sms_sender_guard=receipt.sender)
        return ConversationService(
            calendar, interpreter, HoldService(calendar),
            LifecycleService(calendar, clock), store, clock,
            os.environ["OWNER_NUMBER"], states,
            owner_questions=OwnerCalendarQuestions(calendar, DynamoQuestionContexts(dynamo, table)),
            counteroffers=CounterofferService(
                calendar, store, DynamoCounterofferStore(dynamo, table),
                os.environ["OWNER_NUMBER"]),
            counteroffer_acceptance=CounterofferAcceptance(
                calendar, store, DynamoCounterofferStore(dynamo, table),
                HoldService(calendar)),
            history_reader=store,
        )

    processor = ReceiptProcessor(store, None, business_id, clock,
                                 conversation_factory=conversation_for)
    return process_sqs_batch(event["Records"], processor)
