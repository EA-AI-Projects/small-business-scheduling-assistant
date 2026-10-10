"""Replay-safe processing of persisted, verified SMS receipt references."""

import json
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from scheduling.domain.conversation import (
    OWNER_FAILURE_TEXT,
    ConversationOutcome,
    ConversationService,
)
from scheduling.domain.sms_ingress import InboundReceipt, SenderRole, SmsCommandInterrupted


class ReceiptReader(Protocol):
    def read_received(self, business_id: str, provider_id: str) -> InboundReceipt | None: ...
    def read_reply_text(self, business_id: str, provider_id: str) -> str | None: ...
    def has_committed_command(self, receipt: InboundReceipt) -> bool: ...
    def has_committed_owner_reply_command(self, receipt: InboundReceipt) -> bool: ...
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool: ...
    def claim_processing(self, receipt: InboundReceipt, token: str,
                         now: datetime, lease_until: datetime) -> bool: ...
    def mark_processed(self, receipt: InboundReceipt, token: str, now: datetime) -> None: ...
    def put_reply(self, receipt: InboundReceipt, text: str,
                  token: str, now: datetime) -> bool: ...
    def put_committed_reply(self, receipt: InboundReceipt, outbox_id: str,
                            text: str, token: str) -> bool: ...


class ReceiptProcessor:
    def __init__(self, store: ReceiptReader, conversation: ConversationService | None,
                 business_id: str, clock: Callable[[], datetime],
                 conversation_factory: Callable[[InboundReceipt], ConversationService] | None = None
                 ) -> None:
        if not business_id:
            raise ValueError("Business ID is required")
        if conversation is None and conversation_factory is None:
            raise ValueError("A conversation handler is required")
        self._store = store
        self._conversation = conversation
        self._conversation_factory = conversation_factory
        self._business_id = business_id
        self._clock = clock

    def process(self, business_id: str, provider_id: str) -> ConversationOutcome | None:
        if business_id != self._business_id or not provider_id:
            raise ValueError("Receipt is outside this business")
        receipt = self._store.read_received(business_id, provider_id)
        if receipt is None:
            # Read is consistent; retry transient absence rather than dropping work.
            raise LookupError("Persisted SMS receipt was not found")
        if receipt.business_id != business_id or receipt.provider_id != provider_id:
            raise ValueError("Persisted receipt identity mismatch")
        if not receipt.authorized_for_commands:
            return None
        started_at = self._clock()
        token = uuid4().hex
        # The conditional lease serializes duplicate SQS deliveries. It lasts
        # longer than the Lambda timeout; a crash leaves a retryable receipt.
        if not self._store.claim_processing(receipt, token, started_at,
                                            started_at + timedelta(minutes=2)):
            return None
        # Scheduling writes carry an atomic idempotency result. After a crash
        # between that write and marking the receipt processed, skip re-running
        # a now-terminal command and finish the receipt instead.
        if (receipt.body is None
                or self._store.read_reply_text(business_id, provider_id) is not None):
            self._store.mark_processed(receipt, token, self._clock())
            return None
        if self._store.has_committed_command(receipt):
            if (receipt.role == SenderRole.OWNER
                    and self._store.has_committed_owner_reply_command(receipt)
                    and not self._store.is_opted_out(business_id, receipt.sender)):
                try:
                    self._store.put_reply(receipt, OWNER_FAILURE_TEXT, token, self._clock())
                except SmsCommandInterrupted:
                    self._store.mark_processed(receipt, token, self._clock())
                    return None
                return ConversationOutcome(OWNER_FAILURE_TEXT)
            self._store.mark_processed(receipt, token, self._clock())
            return None
        conversation = (self._conversation_factory(receipt)
                        if self._conversation_factory is not None else self._conversation)
        assert conversation is not None
        try:
            outcome = conversation.handle(receipt)
        except SmsCommandInterrupted:
            if self._store.is_opted_out(business_id, receipt.sender):
                self._store.mark_processed(receipt, token, self._clock())
                return None
            retry_text = (OWNER_FAILURE_TEXT if receipt.role == SenderRole.OWNER else
                          "The schedule changed while I handled that request. Please send it again.")
            self._store.put_reply(receipt, retry_text, token, self._clock())
            return ConversationOutcome(retry_text)
        if outcome.committed:
            if (outcome.client_outbox_id is not None
                    and not self._store.is_opted_out(business_id, receipt.sender)):
                self._store.put_committed_reply(receipt, outcome.client_outbox_id,
                                                outcome.text, token)
            if (outcome.owner_reply_on_commit
                    and not self._store.is_opted_out(business_id, receipt.sender)):
                try:
                    self._store.put_reply(receipt, outcome.text, token, self._clock())
                except SmsCommandInterrupted:
                    self._store.mark_processed(receipt, token, self._clock())
                    return None
            else:
                self._store.mark_processed(receipt, token, self._clock())
        elif self._store.is_opted_out(business_id, receipt.sender):
            self._store.mark_processed(receipt, token, self._clock())
        else:
            try:
                self._store.put_reply(receipt, outcome.text, token, self._clock())
            except SmsCommandInterrupted:
                self._store.mark_processed(receipt, token, self._clock())
                return None
        return outcome


def process_sqs_batch(records: Iterable[dict[str, str]],
                      processor: ReceiptProcessor) -> dict[str, list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    for message in records:
        message_id = message["messageId"]
        try:
            payload = json.loads(message["body"])
            if not isinstance(payload, dict):
                raise TypeError("Receipt queue body must be an object")
            business_id, provider_id = payload.get("business_id"), payload.get("provider_id")
            if not isinstance(business_id, str) or not isinstance(provider_id, str):
                raise TypeError("Receipt queue identity is invalid")
            processor.process(business_id, provider_id)
        except Exception:  # noqa: BLE001 - failed records must reach SQS redrive
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
