"""Body deletion follows the last exchange and preserves receipt metadata."""

from datetime import UTC, datetime, timedelta
from typing import Any

from scheduling.adapters.dynamodb import _command_sort_key
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.sms_ingress import (
    ConsentEvidence,
    InboundReceipt,
    Keyword,
    SenderRole,
    SmsCommandInterrupted,
)
from scheduling.domain.sms_status import SmsDeliveryStatus

NOW = datetime(2026, 9, 28, 17, tzinfo=UTC)


class MemoryDynamo:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self.items.get(kwargs["Key"]["SK"]["S"])
        return {"Item": item} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]["S"]
        return {"Items": [item for key, item in self.items.items() if key.startswith(prefix)]}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        # Apply each transaction to a copy so a failed condition has no effects.
        items = {key: dict(value) for key, value in self.items.items()}
        for action in kwargs["TransactItems"]:
            if "Put" in action:
                item = action["Put"]["Item"]
                key = item["SK"]["S"]
                if key in items and "ConditionExpression" in action["Put"]:
                    raise RuntimeError("duplicate receipt")
                items[key] = dict(item)
            elif "ConditionCheck" in action:
                check = action["ConditionCheck"]
                key = check["Key"]["SK"]["S"]
                if check["ConditionExpression"] == "attribute_exists(PK)":
                    if key not in items:
                        raise TransactionCancelled()
                    continue
                if check["ConditionExpression"] == "attribute_not_exists(PK)":
                    if key in items:
                        raise TransactionCancelled()
                    continue
                if "attribute_not_exists(PK) OR attribute_exists(cleared_at)" == check[
                        "ConditionExpression"]:
                    if key in items and "cleared_at" not in items[key]:
                        raise TransactionCancelled()
                    continue
                clock = ("last_program_text_at" if "last_program_text_at" in
                         items.get(key, {}) else "last_exchange_at")
                if (key in items and clock in items[key] and
                        items[key][clock]["S"] > check["ExpressionAttributeValues"]
                        [":cutoff"]["S"]):
                    raise RuntimeError("conditional conflict")
            elif "Delete" in action:
                delete = action["Delete"]
                key = delete["Key"]["SK"]["S"]
                if "ConditionExpression" in delete:
                    old = items.get(key)
                    field = next(name for name in
                                 ("opted_out_at", "suppressed_at", "agreed_at")
                                 if name in delete["ConditionExpression"])
                    expected = delete["ExpressionAttributeValues"].get(
                        ":recorded", delete["ExpressionAttributeValues"].get(":at"))
                    if (old is None or "legal_hold_reason" in old or
                            old[field] != expected):
                        raise RuntimeError("conditional conflict")
                items.pop(key, None)
            else:
                update = action["Update"]
                key = update["Key"]["SK"]["S"]
                if "processing_token = :token" in update.get("ConditionExpression", ""):
                    current = items.get(key)
                    if (current is None or current.get("processing_token") !=
                            update["ExpressionAttributeValues"][":token"] or
                            "processed_at" in current or "reply_text" in current):
                        raise RuntimeError("receipt lease conflict")
                if update["UpdateExpression"] == "REMOVE body, reply_text":
                    items[key].pop("body", None)
                    items[key].pop("reply_text", None)
                elif update["UpdateExpression"] == "REMOVE body":
                    current = items[key]
                    values = update["ExpressionAttributeValues"]
                    if (current["body"] != values[":body"] or
                            current["sent_at"]["S"] > values[":cutoff"]["S"] or
                            ("attribute_not_exists(legal_hold_reason)" in
                             update["ConditionExpression"] and
                             "legal_hold_reason" in current)):
                        raise TransactionCancelled()
                    current.pop("body")
                else:
                    current = items.setdefault(key, {"SK": {"S": key}})
                    set_part, _, remove_part = update["UpdateExpression"].partition(" REMOVE ")
                    for assignment in set_part.removeprefix("SET ").split(", "):
                        field, value = assignment.split(" = ")
                        field = update.get("ExpressionAttributeNames", {}).get(field, field)
                        incoming = update["ExpressionAttributeValues"][value]
                        if (field in {"last_exchange_at", "last_program_text_at"} and
                                current.get(field, {}).get("S", "") > incoming["S"]):
                            raise RuntimeError("conditional conflict")
                        current[field] = incoming
                    if remove_part:
                        for field in remove_part.split(", "):
                            current.pop(field, None)
        self.items = items
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]["SK"]["S"]
        values = kwargs.get("ExpressionAttributeValues", {})
        expression = kwargs["UpdateExpression"]
        if "processing_token = :token" in expression:
            current = self.items.get(key)
            if (current is None or not current["authorized_for_commands"]["BOOL"] or
                    "processed_at" in current or
                    ("processing_lease_until" in current and
                     current["processing_lease_until"]["S"] >= values[":now"]["S"])):
                raise RuntimeError("receipt lease conflict")
            current["processing_token"] = values[":token"]
            current["processing_lease_until"] = values[":until"]
            return {}
        if expression.startswith("SET processed_at = :now"):
            current = self.items[key]
            if (current.get("processing_token") != values[":token"] or
                    "processed_at" in current):
                raise RuntimeError("receipt lease conflict")
            current["processed_at"] = values[":now"]
            current.pop("processing_token", None)
            current.pop("processing_lease_until", None)
            return {}
        if "legal_hold_reason" in kwargs["UpdateExpression"]:
            if kwargs["UpdateExpression"].startswith("SET"):
                self.items[key]["legal_hold_reason"] = values[":reason"]
            else:
                self.items[key].pop("legal_hold_reason", None)
            return {}
        if expression == "REMOVE manual_message":
            current = self.items[key]
            if (current["manual_message"] != values[":body"]
                    or current["run_at"]["S"] > values[":cutoff"]["S"]):
                raise RuntimeError("retention race")
            current.pop("manual_message")
            return {}
        previous = self.items.get(key)
        rank = int(values[":rank"]["N"])
        if previous is not None and int(previous["status_rank"]["N"]) > rank:
            return {}
        self.items[key] = {
            "SK": {"S": key}, "status_rank": values[":rank"],
            "delivery_status": values[":status"], "recipient": values[":recipient"],
            "observed_at": values[":at"], "outbox_id": values[":outbox"],
            "provider_id": values[":provider"],
        }
        if ":error" in values:
            self.items[key]["error_code"] = values[":error"]
        return {}


class TransactionCancelled(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "TransactionCanceledException"}}


class HoldBeforeOutboundPurge(MemoryDynamo):
    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        if any(action.get("Update", {}).get("UpdateExpression") == "REMOVE body"
               for action in kwargs["TransactItems"]):
            for action in kwargs["TransactItems"]:
                if "Update" in action:
                    self.items[action["Update"]["Key"]["SK"]["S"]][
                        "legal_hold_reason"] = {"S": "synthetic hold"}
        return super().transact_write_items(**kwargs)


class NewTextBeforePurge(MemoryDynamo):
    def __init__(self) -> None:
        super().__init__()
        self.inject = False

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        if self.inject and "ConditionCheck" in kwargs["TransactItems"][0]:
            self.inject = False
            self.items["SMS_THREAD#+14155550101"]["last_program_text_at"] = {
                "S": NOW.isoformat(timespec="microseconds")}
            raise TransactionCancelled()
        return super().transact_write_items(**kwargs)


class NewHoldBeforePurge(MemoryDynamo):
    def __init__(self) -> None:
        super().__init__()
        self.inject = False

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        if self.inject and "ConditionCheck" in kwargs["TransactItems"][0]:
            self.inject = False
            key = next(action["Delete"]["Key"]["SK"]["S"]
                       for action in kwargs["TransactItems"] if "Delete" in action)
            self.items[key]["legal_hold_reason"] = {"S": "documented case"}
            raise TransactionCancelled()
        return super().transact_write_items(**kwargs)


def _receipt(sid: str, when: datetime) -> InboundReceipt:
    return InboundReceipt("pilot", sid, "+14155550101", "+14155550000", "Next Tuesday",
                          when, SenderRole.CLIENT, "client-1", Keyword.OTHER, True)


def test_body_waits_until_90_days_after_last_exchange() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    first = NOW - timedelta(days=100)
    latest = NOW - timedelta(days=20)
    assert store.put_received(_receipt("SM-first", first)) is True
    assert store.put_received(_receipt("SM-last", latest)) is True
    assert store.purge_expired_bodies("pilot", NOW) == 0
    assert dynamo.items["SMS#SM-first"]["body"]["S"] == "Next Tuesday"
    assert store.purge_expired_bodies("pilot", latest + timedelta(days=90)) == 2
    assert "body" not in dynamo.items["SMS#SM-first"]
    assert "provider_id" in dynamo.items["SMS#SM-first"]
    assert store.put_received(_receipt("SM-first", first)) is False


def test_outbound_reply_extends_last_exchange_window() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    first = NOW - timedelta(days=100)
    outbound = NOW - timedelta(days=20)
    store.put_received(_receipt("SM-first", first))
    store.record_outbound("pilot", "+14155550101", "SM-out", outbound)
    assert store.purge_expired_bodies("pilot", NOW) == 0
    assert store.purge_expired_bodies("pilot", outbound + timedelta(days=90)) == 1
    assert "body" not in dynamo.items["SMS#SM-first"]
    assert dynamo.items["SMS_OUT#SM-out"]["recipient"]["S"] == "+14155550101"


def test_outbound_body_is_retained_with_active_thread_then_erased() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    sent = NOW - timedelta(days=100)
    store.record_outbound("pilot", "+14155550101", "SM-out", sent,
                          "Exact sent invitation", "client-1", "booking_invitation")
    assert dynamo.items["SMS_OUT#SM-out"]["body"]["S"] == "Exact sent invitation"
    store.put_received(_receipt("SM-new", NOW - timedelta(days=1)))
    assert store.purge_expired_outbound_bodies("pilot", NOW, "+14155550100") == 0
    assert store.purge_expired_outbound_bodies(
        "pilot", NOW + timedelta(days=90), "+14155550100") == 1
    assert "body" not in dynamo.items["SMS_OUT#SM-out"]


def test_outbound_purge_loses_to_new_hold_for_owner_and_client() -> None:
    for recipient, client_id in (("+14155550100", None), ("+14155550101", "client-1")):
        dynamo = HoldBeforeOutboundPurge()
        store = DynamoSmsIngressStore(dynamo, "synthetic")
        store.record_outbound("pilot", recipient, "SM-held", NOW - timedelta(days=100),
                              "Synthetic sent text", client_id)
        assert store.purge_expired_outbound_bodies("pilot", NOW, "+14155550100") == 0
        assert dynamo.items["SMS_OUT#SM-held"]["body"]["S"] == "Synthetic sent text"


def test_conversation_reply_is_atomic_idempotent_and_purged_with_sms_body() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    received = _receipt("SM-reply", NOW - timedelta(days=91))
    assert store.put_received(received)
    assert store.read_received("pilot", "SM-reply") == received
    assert store.claim_processing(received, "token-1", received.received_at,
                                  received.received_at + timedelta(minutes=2))
    assert store.put_reply(received, "Please send one exact date.", "token-1",
                           received.received_at)
    assert not store.put_reply(received, "Changed answer", "token-1",
                               received.received_at)
    assert store.read_reply_text("pilot", "SM-reply") == "Please send one exact date."
    intent = dynamo.items["OUTBOX#sms-reply#SM-reply"]
    assert intent["recipient"]["S"] == "client"
    assert intent["template"]["S"] == "conversation-reply"
    assert intent["entity_id"]["S"] == "SM-reply"
    assert store.purge_expired_bodies("pilot", NOW) == 1
    assert store.read_reply_text("pilot", "SM-reply") is None
    assert store.read_received("pilot", "SM-reply").body is None  # type: ignore[union-attr]


def test_committed_command_lookup_uses_the_same_idempotency_key_as_scheduler() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    received = _receipt("SM-book", NOW)
    assert not store.has_committed_command(received)
    key = _command_sort_key("client-1", "create_hold", "SM-book")
    dynamo.items[key] = {"SK": {"S": key}}
    assert store.has_committed_command(received)


def test_stop_before_reply_transaction_prevents_reply_intent() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    received = _receipt("SM-stopped", NOW)
    assert store.put_received(received)
    assert store.claim_processing(received, "token-1", NOW, NOW + timedelta(minutes=2))
    dynamo.items[f"SMS_SUPPRESS#{received.sender}"] = {
        "SK": {"S": f"SMS_SUPPRESS#{received.sender}"}}

    try:
        store.put_reply(received, "Please send one exact date.", "token-1", NOW)
    except SmsCommandInterrupted:
        pass
    else:
        raise AssertionError("STOP must block a newly committed reply")
    assert "OUTBOX#sms-reply#SM-stopped" not in dynamo.items


def test_failed_provider_callback_is_linked_for_owner_follow_up() -> None:
    dynamo = MemoryDynamo()
    dynamo.items["OUTBOX#outbox-1"] = {"SK": {"S": "OUTBOX#outbox-1"}}
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    store.put_status(SmsDeliveryStatus("pilot", "outbox-1", "SM-failed",
                                       "undelivered", "+14155550101", NOW, "30007"))
    failures = store.list_delivery_failures("pilot")
    assert len(failures) == 1
    assert failures[0].outbox_id == "outbox-1"
    assert failures[0].error_code == "30007"


def test_late_provider_callback_cannot_recreate_evidence_after_outbox_erasure() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    store.put_status(SmsDeliveryStatus("pilot", "erased-outbox", "SM-late",
                                       "undelivered", "+14155550101", NOW, "30007"))
    assert "SMS_STATUS#SM-late" not in dynamo.items


def _consent(when: datetime) -> ConsentEvidence:
    return ConsentEvidence("pilot", "client-1", "Synthetic Person", "+14155550101",
                           when, "pilot-v1")


def test_evidence_expires_four_years_after_latest_inbound_or_outbound() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    old = NOW.replace(year=NOW.year - 5)
    recent = NOW.replace(year=NOW.year - 3)
    store.put_consent(_consent(old))
    store.put_received(_receipt("SM-inbound", recent))
    assert store.purge_expired_evidence("pilot", NOW) == 0
    store.record_outbound("pilot", "+14155550101", "SM-out", NOW)
    assert store.purge_expired_evidence("pilot", NOW.replace(year=NOW.year + 3)) == 0
    assert store.purge_expired_evidence("pilot", NOW.replace(year=NOW.year + 4)) == 2
    assert store.read_consent("pilot", "+14155550101") is None


def test_evidence_hold_and_concurrent_text_prevent_deletion() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    old = NOW.replace(year=NOW.year - 5)
    store.put_consent(_consent(old))
    history = next(key for key in dynamo.items if key.startswith("SMS_CONSENT#"))
    store.set_evidence_legal_hold("pilot", history, "documented case")
    assert store.purge_expired_evidence("pilot", NOW) == 1
    assert history in dynamo.items
    store.record_outbound("pilot", "+14155550101", "SM-new", NOW)
    store.set_evidence_legal_hold("pilot", history, None)
    assert store.purge_expired_evidence("pilot", NOW) == 0
    assert history in dynamo.items


def test_purge_racing_new_program_text_preserves_evidence() -> None:
    dynamo = NewTextBeforePurge()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    old = NOW.replace(year=NOW.year - 5)
    store.put_consent(_consent(old))
    dynamo.items["SMS_THREAD#+14155550101"] = {
        "SK": {"S": "SMS_THREAD#+14155550101"},
        "last_program_text_at": {"S": old.isoformat(timespec="microseconds")},
    }
    dynamo.inject = True
    assert store.purge_expired_evidence("pilot", NOW) == 0
    assert store.read_consent("pilot", "+14155550101") is not None


def test_purge_racing_new_legal_hold_preserves_evidence() -> None:
    dynamo = NewHoldBeforePurge()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    store.put_consent(_consent(NOW.replace(year=NOW.year - 5)))
    dynamo.inject = True
    assert store.purge_expired_evidence("pilot", NOW) == 1
    assert len([item for item in dynamo.items.values()
                if "legal_hold_reason" in item]) == 1


def test_legacy_exchange_clock_prevents_early_evidence_purge() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    store.put_consent(_consent(NOW.replace(year=NOW.year - 5)))
    dynamo.items["SMS_THREAD#+14155550101"] = {
        "SK": {"S": "SMS_THREAD#+14155550101"},
        "last_exchange_at": {"S": NOW.replace(year=NOW.year - 1).isoformat(
            timespec="microseconds")},
    }
    assert store.purge_expired_evidence("pilot", NOW) == 0
    assert store.read_consent("pilot", "+14155550101") is not None


def test_start_keeps_held_stop_evidence_while_clearing_suppression() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    old = NOW.replace(year=NOW.year - 5)
    store.put_received(InboundReceipt(
        "pilot", "SM-held-stop", "+14155550101", "+14155550000", None,
        old, SenderRole.CLIENT, "client-1", Keyword.STOP, False,
    ))
    optout_key = "SMS_OPTOUT#+14155550101"
    store.set_evidence_legal_hold("pilot", optout_key, "documented case")
    store.put_consent(_consent(NOW))
    store.put_received(InboundReceipt(
        "pilot", "SM-held-start", "+14155550101", "+14155550000", None,
        NOW + timedelta(seconds=1), SenderRole.CLIENT, "client-1", Keyword.START, False,
    ))
    assert "legal_hold_reason" in dynamo.items[optout_key]
    assert not store.is_opted_out("pilot", "+14155550101")


def test_legacy_stop_without_suppression_marker_still_blocks_delivery() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    dynamo.items["SMS_OPTOUT#+14155550101"] = {
        "SK": {"S": "SMS_OPTOUT#+14155550101"},
        "phone_e164": {"S": "+14155550101"},
        "opted_out_at": {"S": NOW.replace(year=NOW.year - 5).isoformat(
            timespec="microseconds")},
    }
    assert store.is_opted_out("pilot", "+14155550101")
    assert store.purge_expired_evidence("pilot", NOW) == 1
    assert "SMS_OPTOUT#+14155550101" not in dynamo.items
    assert store.is_opted_out("pilot", "+14155550101")


def test_second_stop_does_not_overwrite_held_stop_evidence() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    old = NOW.replace(year=NOW.year - 1)
    for sid, when in (("SM-first-stop", old), ("SM-second-stop", NOW)):
        if sid == "SM-second-stop":
            store.set_evidence_legal_hold("pilot", "SMS_OPTOUT#+14155550101",
                                          "documented case")
        store.put_received(InboundReceipt(
            "pilot", sid, "+14155550101", "+14155550000", None,
            when, SenderRole.CLIENT, "client-1", Keyword.STOP, False,
        ))
    assert dynamo.items["SMS_OPTOUT#+14155550101"]["opted_out_at"]["S"] == (
        old.isoformat(timespec="microseconds"))
    assert "SMS_OPTOUT_EVENT#+14155550101#SM-second-stop" in dynamo.items
    assert store.is_opted_out("pilot", "+14155550101")


def test_old_stop_evidence_purges_but_suppression_remains() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    old = NOW.replace(year=NOW.year - 5)
    stop = InboundReceipt("pilot", "SM-stop", "+14155550101", "+14155550000",
                          None, old, SenderRole.CLIENT, "client-1", Keyword.STOP, False)
    store.put_received(stop)
    assert store.is_opted_out("pilot", "+14155550101")
    assert store.purge_expired_evidence("pilot", NOW) == 2
    assert "SMS_OPTOUT#+14155550101" not in dynamo.items
    assert "SMS_SUPPRESS#+14155550101" in dynamo.items
    assert store.is_opted_out("pilot", "+14155550101")
    store.put_consent(_consent(NOW))
    start = InboundReceipt("pilot", "SM-start", "+14155550101", "+14155550000",
                           None, NOW + timedelta(seconds=1), SenderRole.CLIENT,
                           "client-1", Keyword.START, False)
    store.put_received(start)
    assert not store.is_opted_out("pilot", "+14155550101")


OWNER = "+14155550100"


def _owner_receipt(sid: str, when: datetime) -> InboundReceipt:
    return InboundReceipt("pilot", sid, OWNER, "+14155550000", "Move the Thursday visit",
                          when, SenderRole.OWNER, None, Keyword.OTHER, True)


def test_owner_text_expires_90_days_after_its_own_time_not_thread_activity() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    old = _owner_receipt("SM-owner-old", NOW - timedelta(days=100))
    recent = _owner_receipt("SM-owner-new", NOW - timedelta(days=1))
    store.put_received(old)
    store.put_received(recent)
    store.put_received(_receipt("SM-client-old", NOW - timedelta(days=100)))
    store.put_received(_receipt("SM-client-new", NOW - timedelta(days=1)))
    assert store.purge_expired_bodies("pilot", NOW, OWNER) == 1
    assert "body" not in dynamo.items["SMS#SM-owner-old"]
    assert dynamo.items["SMS#SM-owner-new"]["body"]["S"] == "Move the Thursday visit"
    # The client thread's latest exchange still keeps its older text.
    assert dynamo.items["SMS#SM-client-old"]["body"]["S"] == "Next Tuesday"
    # Redelivery stays idempotent, and a read after the purge shows no body.
    assert store.put_received(old) is False
    assert store.read_received("pilot", "SM-owner-old").body is None  # type: ignore[union-attr]


def test_owner_rule_applies_only_when_the_owner_number_is_given() -> None:
    dynamo = MemoryDynamo()
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    store.put_received(_owner_receipt("SM-owner-old", NOW - timedelta(days=100)))
    store.put_received(_owner_receipt("SM-owner-new", NOW - timedelta(days=1)))
    assert store.purge_expired_bodies("pilot", NOW) == 0


def test_retention_handler_normalizes_owner_number_and_fails_closed(
        monkeypatch: Any) -> None:
    from scheduling.workers import sms_retention

    seen: list[str | None] = []

    class Store:
        def __init__(self, *_args: Any) -> None: ...

        def purge_expired_bodies(self, _b: str, _n: datetime, owner: str | None) -> int:
            seen.append(owner)
            return 0

        def purge_expired_outbound_bodies(self, _b: str, _n: datetime,
                                          owner: str) -> int:
            return 0

        def purge_expired_manual_invitation_bodies(self, _b: str, _n: datetime) -> int:
            return 0

        def purge_expired_evidence(self, _b: str, _n: datetime) -> int:
            return 0

    monkeypatch.setattr(sms_retention.boto3, "client", lambda *_a: object())
    monkeypatch.setattr(sms_retention, "DynamoSmsIngressStore", Store)
    monkeypatch.setenv("SCHEDULING_TABLE_NAME", "synthetic")
    monkeypatch.setenv("BUSINESS_ID", "pilot")
    monkeypatch.setenv("OWNER_NUMBER", " +1 (415) 555-0100 ")
    sms_retention.handler({}, None)
    assert seen == ["+14155550100"]
    for bad in ("", "   ", "4155550100"):
        monkeypatch.setenv("OWNER_NUMBER", bad)
        try:
            sms_retention.handler({}, None)
        except ValueError:
            continue
        raise AssertionError("malformed owner number must fail closed")
    assert seen == ["+14155550100"]


def test_manual_invitation_text_is_purged_after_ninety_days() -> None:
    dynamo = MemoryDynamo()
    old = "INVITATION#old"
    recent = "INVITATION#recent"
    dynamo.items[old] = {
        "PK": {"S": "BUSINESS#pilot"}, "SK": {"S": old},
        "run_at": {"S": (NOW - timedelta(days=90)).isoformat(timespec="microseconds")},
        "manual_message": {"S": "Book or STOP"}, "state": {"S": "SENT"},
    }
    dynamo.items[recent] = {
        "PK": {"S": "BUSINESS#pilot"}, "SK": {"S": recent},
        "run_at": {"S": (NOW - timedelta(days=89)).isoformat(timespec="microseconds")},
        "manual_message": {"S": "Book later or STOP"},
    }
    store = DynamoSmsIngressStore(dynamo, "synthetic")
    assert store.purge_expired_manual_invitation_bodies("pilot", NOW) == 1
    assert "manual_message" not in dynamo.items[old]
    assert dynamo.items[old]["state"]["S"] == "SENT"
    assert dynamo.items[recent]["manual_message"]["S"] == "Book later or STOP"
