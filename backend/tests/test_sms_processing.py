"""The queue carries only persisted receipt IDs and retries failures per record."""

import json
from datetime import UTC, datetime

import pytest

from scheduling.adapters.sms_queue import SQSReceiptQueue
from scheduling.domain.conversation import ConversationOutcome
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole, SmsCommandInterrupted
from scheduling.domain.sms_processing import ReceiptProcessor, process_sqs_batch


class QueueClient:
    def __init__(self) -> None:
        self.kwargs: dict[str, str] = {}

    def send_message(self, **kwargs: str) -> dict[str, str]:
        self.kwargs = kwargs
        return {"MessageId": "synthetic"}


class UnacceptedQueueClient(QueueClient):
    def send_message(self, **kwargs: str) -> dict[str, str]:
        self.kwargs = kwargs
        return {}


def test_queue_payload_has_only_receipt_identity() -> None:
    client = QueueClient()
    queue = SQSReceiptQueue(client, "https://sqs.example.test/receipts")
    queue.enqueue("pilot", "SM-1")
    assert json.loads(client.kwargs["MessageBody"]) == {
        "business_id": "pilot", "provider_id": "SM-1",
    }
    assert "body" not in client.kwargs["MessageBody"]
    with pytest.raises(RuntimeError, match="did not accept"):
        SQSReceiptQueue(UnacceptedQueueClient(), "synthetic-url").enqueue("pilot", "SM-2")


class Reader:
    def __init__(self, receipts: dict[tuple[str, str], InboundReceipt]) -> None:
        self.receipts = receipts
        self.replies: list[tuple[str, str]] = []
        self.committed: set[str] = set()
        self.processing: dict[str, str] = {}
        self.processed: set[str] = set()
        self.opted_out = False
        self.committed_drafts: list[tuple[str, str, str]] = []
        self.accept_committed_draft = True

    def read_received(self, business_id: str, provider_id: str) -> InboundReceipt | None:
        return self.receipts.get((business_id, provider_id))

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return self.opted_out

    def read_reply_text(self, business_id: str, provider_id: str) -> str | None:
        return next((text for sid, text in self.replies if sid == provider_id), None)

    def has_committed_command(self, receipt: InboundReceipt) -> bool:
        return receipt.provider_id in self.committed

    def claim_processing(self, receipt: InboundReceipt, token: str,
                         now: datetime, lease_until: datetime) -> bool:
        if receipt.provider_id in self.processed:
            return False
        if receipt.provider_id in self.processing:
            raise RuntimeError("receipt busy")
        self.processing[receipt.provider_id] = token
        return True

    def mark_processed(self, receipt: InboundReceipt, token: str, now: datetime) -> None:
        assert self.processing.pop(receipt.provider_id) == token
        self.processed.add(receipt.provider_id)

    def put_reply(self, receipt: InboundReceipt, text: str,
                  token: str, now: datetime) -> bool:
        assert self.processing.pop(receipt.provider_id) == token
        self.replies.append((receipt.provider_id, text))
        self.processed.add(receipt.provider_id)
        return True

    def put_committed_reply(self, receipt: InboundReceipt, outbox_id: str,
                            text: str, token: str) -> bool:
        assert self.processing[receipt.provider_id] == token
        if not self.accept_committed_draft:
            return False
        self.committed_drafts.append((receipt.provider_id, outbox_id, text))
        self.replies.append((receipt.provider_id, text))
        return True


class Conversation:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def handle(self, receipt: object) -> ConversationOutcome:
        self.calls.append(receipt)
        return ConversationOutcome("safe reply")


def test_committed_draft_overrides_one_existing_notification_or_falls_back() -> None:
    receipt = InboundReceipt("pilot", "SM-commit", "+14155550101", "+14155550000",
                             "YES", datetime(2026, 9, 29, tzinfo=UTC),
                             SenderRole.CLIENT, "client-1", Keyword.OTHER, True)

    class Booked(Conversation):
        def handle(self, receipt: object) -> ConversationOutcome:
            self.calls.append(receipt)
            return ConversationOutcome("Pending owner approval. Ref a101a101.", True,
                                       "a101a101", "a101a101#client")

    for accepts in (True, False):
        reader = Reader({("pilot", "SM-commit"): receipt})
        reader.accept_committed_draft = accepts
        conversation = Booked()
        processor = ReceiptProcessor(reader, conversation, "pilot",  # type: ignore[arg-type]
                                     lambda: datetime(2026, 9, 29, tzinfo=UTC))
        assert processor.process("pilot", "SM-commit") is not None
        assert len(reader.committed_drafts) == int(accepts)
        assert processor.process("pilot", "SM-commit") is None
        assert len(conversation.calls) == 1
        assert len(reader.committed_drafts) == int(accepts)

    reader = Reader({("pilot", "SM-commit"): receipt})
    reader.opted_out = True
    processor = ReceiptProcessor(reader, Booked(), "pilot",  # type: ignore[arg-type]
                                 lambda: datetime(2026, 9, 29, tzinfo=UTC))
    assert processor.process("pilot", "SM-commit") is not None
    assert reader.committed_drafts == []


def test_processor_reads_persisted_receipt_and_rejects_missing_or_wrong_scope() -> None:
    receipt = InboundReceipt("pilot", "SM-1", "+14155550101", "+14155550000",
                             "Book 2026-10-01", datetime(2026, 9, 29, tzinfo=UTC),
                             SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    conversation = Conversation()
    reader = Reader({("pilot", "SM-1"): receipt})
    processor = ReceiptProcessor(reader, conversation, "pilot",  # type: ignore[arg-type]
                                 lambda: datetime(2026, 9, 29, tzinfo=UTC))
    assert processor.process("pilot", "SM-1") == ConversationOutcome("safe reply")
    assert conversation.calls == [receipt]
    assert reader.replies == [("SM-1", "safe reply")]
    with pytest.raises(ValueError):
        processor.process("other", "SM-1")
    with pytest.raises(LookupError):
        processor.process("pilot", "SM-missing")


def test_replay_after_committed_command_does_not_create_contradictory_reply() -> None:
    received = InboundReceipt("pilot", "SM-approved", "+14155559999", "+14155550000",
                              "Approve abcdef12", datetime(2026, 9, 29, tzinfo=UTC),
                              SenderRole.OWNER, None, Keyword.OTHER, True)

    class DecisionConversation(Conversation):
        def handle(self, receipt: InboundReceipt) -> ConversationOutcome:
            self.calls.append(receipt)
            reader.committed.add(receipt.provider_id)
            return ConversationOutcome("Request confirmed.", True, "appointment-1")

    reader = Reader({("pilot", "SM-approved"): received})
    conversation = DecisionConversation()
    processor = ReceiptProcessor(reader, conversation, "pilot",  # type: ignore[arg-type]
                                 lambda: datetime(2026, 9, 29, tzinfo=UTC))
    assert processor.process("pilot", "SM-approved").committed  # type: ignore[union-attr]
    assert processor.process("pilot", "SM-approved") is None
    assert conversation.calls == [received]
    assert reader.replies == []


def test_concurrent_duplicate_cannot_reply_after_first_worker_commits() -> None:
    received = InboundReceipt("pilot", "SM-race", "+14155559999", "+14155550000",
                              "Approve abcdef12", datetime(2026, 9, 29, tzinfo=UTC),
                              SenderRole.OWNER, None, Keyword.OTHER, True)
    reader = Reader({("pilot", "SM-race"): received})

    class RacingConversation(Conversation):
        def handle(self, receipt: InboundReceipt) -> ConversationOutcome:
            self.calls.append(receipt)
            with pytest.raises(RuntimeError, match="busy"):
                second.process("pilot", "SM-race")
            reader.committed.add(receipt.provider_id)
            return ConversationOutcome("Request confirmed.", True, "appointment-1")

    conversation = RacingConversation()
    clock = lambda: datetime(2026, 9, 29, tzinfo=UTC)
    first = ReceiptProcessor(reader, conversation, "pilot", clock)  # type: ignore[arg-type]
    second = ReceiptProcessor(reader, conversation, "pilot", clock)  # type: ignore[arg-type]
    assert first.process("pilot", "SM-race").committed  # type: ignore[union-attr]
    assert second.process("pilot", "SM-race") is None
    assert reader.replies == []


def test_partial_batch_failure_preserves_only_failed_ids() -> None:
    class Processor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def process(self, business_id: str, provider_id: str) -> None:
            self.calls.append((business_id, provider_id))
            if provider_id == "SM-fail":
                raise RuntimeError("synthetic transient failure")

    processor = Processor()
    records = [
        {"messageId": "ok", "body": json.dumps({"business_id": "pilot",
                                                 "provider_id": "SM-good"})},
        {"messageId": "bad", "body": json.dumps({"business_id": "pilot",
                                                  "provider_id": "SM-fail"})},
        {"messageId": "malformed", "body": "not-json"},
    ]
    assert process_sqs_batch(records, processor) == {  # type: ignore[arg-type]
        "batchItemFailures": [{"itemIdentifier": "bad"}, {"itemIdentifier": "malformed"}],
    }
    assert processor.calls == [("pilot", "SM-good"), ("pilot", "SM-fail")]


def test_stop_during_command_is_terminal_even_if_opt_out_later_clears() -> None:
    receipt = InboundReceipt("pilot", "SM-stop-race", "+14155550101", "+14155550000",
                             "BOOK 2026-10-01 09:00", datetime(2026, 9, 29, tzinfo=UTC),
                             SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    reader = Reader({("pilot", receipt.provider_id): receipt})

    class StoppedConversation(Conversation):
        def handle(self, receipt: InboundReceipt) -> ConversationOutcome:
            self.calls.append(receipt)
            reader.opted_out = True
            raise SmsCommandInterrupted

    conversation = StoppedConversation()
    clock = lambda: datetime(2026, 9, 29, tzinfo=UTC)
    processor = ReceiptProcessor(reader, conversation, "pilot", clock)  # type: ignore[arg-type]
    assert processor.process("pilot", receipt.provider_id) is None
    reader.opted_out = False
    assert processor.process("pilot", receipt.provider_id) is None
    assert len(conversation.calls) == 1
    assert reader.replies == []


def test_stop_during_reply_commit_is_terminal_even_if_opt_out_later_clears() -> None:
    receipt = InboundReceipt("pilot", "SM-reply-race", "+14155550101", "+14155550000",
                             "Next Tuesday", datetime(2026, 9, 29, tzinfo=UTC),
                             SenderRole.CLIENT, "client-1", Keyword.OTHER, True)

    class ReplyRaceReader(Reader):
        def put_reply(self, receipt: InboundReceipt, text: str,
                      token: str, now: datetime) -> bool:
            self.opted_out = True
            raise SmsCommandInterrupted

    reader = ReplyRaceReader({("pilot", receipt.provider_id): receipt})
    conversation = Conversation()
    clock = lambda: datetime(2026, 9, 29, tzinfo=UTC)
    processor = ReceiptProcessor(reader, conversation, "pilot", clock)  # type: ignore[arg-type]
    assert processor.process("pilot", receipt.provider_id) is None
    reader.opted_out = False
    assert processor.process("pilot", receipt.provider_id) is None
    assert len(conversation.calls) == 1
    assert reader.replies == []


def test_cloud_worker_fails_closed_before_accessing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from scheduling.workers.sms_conversation import handler

    monkeypatch.delenv("SMS_CONVERSATION_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="not authorized"):
        handler({"Records": []}, None)
