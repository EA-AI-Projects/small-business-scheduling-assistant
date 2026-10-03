"""Conditional SMS ingress and consent records in the business table."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol

from scheduling.adapters.dynamodb import _command_sort_key, _record_transaction_conflict
from scheduling.adapters.outbox_aws import due_keys
from scheduling.domain.client_records import ClientProfile, RecordConflict
from scheduling.domain.outbox import DeliveryState, OutboxRecord
from scheduling.domain.sms_ingress import (
    ConsentEvidence,
    InboundReceipt,
    Keyword,
    SenderRole,
    SmsCommandInterrupted,
    SmsIngressStore,
    normalize_phone,
)
from scheduling.domain.sms_status import STATUS_RANK, SmsDeliveryStatus


class SmsDynamoClient(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]: ...
    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def query(self, **kwargs: Any) -> dict[str, Any]: ...


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _four_year_cutoff(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("Retention clock must be timezone-aware")
    current = now.astimezone(UTC)
    try:
        return _instant(current.replace(year=current.year - 4))
    except ValueError:  # February 29 has no fourth anniversary in a non-leap year.
        return _instant(current.replace(year=current.year - 4, day=28))


class DynamoSmsIngressStore(SmsIngressStore):
    def __init__(self, client: SmsDynamoClient, table_name: str) -> None:
        self._client = client
        self._table = table_name

    @staticmethod
    def _key(business_id: str, sort_key: str) -> dict[str, Any]:
        return {"PK": {"S": f"BUSINESS#{business_id}"}, "SK": {"S": sort_key}}

    def _get(self, business_id: str, sort_key: str) -> dict[str, Any] | None:
        return self._client.get_item(TableName=self._table,
                                     Key=self._key(business_id, sort_key),
                                     ConsistentRead=True).get("Item")

    def _erasure_check(self, business_id: str, client_id: str) -> dict[str, Any]:
        return {"ConditionCheck": {
            "TableName": self._table,
            "Key": self._key(business_id, f"ERASURE#{sha256(client_id.encode()).hexdigest()}"),
            "ConditionExpression": "attribute_not_exists(PK)",
        }}

    def _phone_erasure_check(self, business_id: str, phone: str) -> dict[str, Any]:
        return {"ConditionCheck": {
            "TableName": self._table,
            "Key": self._key(business_id, f"ERASURE_PHONE#{phone}"),
            "ConditionExpression": "attribute_not_exists(PK)",
        }}

    def read_received(self, business_id: str, provider_id: str) -> InboundReceipt | None:
        item = self._get(business_id, f"SMS#{provider_id}")
        if item is None:
            return None
        return InboundReceipt(
            business_id, item["provider_id"]["S"], item["sender"]["S"],
            item["recipient"]["S"], item.get("body", {}).get("S"),
            datetime.fromisoformat(item["received_at"]["S"]),
            SenderRole(item["role"]["S"]), item.get("client_id", {}).get("S"),
            Keyword(item["keyword"]["S"]), item["authorized_for_commands"]["BOOL"],
        )

    def read_reply_text(self, business_id: str, provider_id: str) -> str | None:
        item = self._get(business_id, f"SMS#{provider_id}")
        return item.get("reply_text", {}).get("S") if item is not None else None

    def has_committed_command(self, receipt: InboundReceipt) -> bool:
        return any(self._get(receipt.business_id, key) is not None
                   for key in self._command_keys(receipt))

    @staticmethod
    def _command_keys(receipt: InboundReceipt) -> tuple[str, ...]:
        actor_id = (receipt.sender if receipt.role == SenderRole.OWNER else
                    receipt.client_id if receipt.role == SenderRole.CLIENT else None)
        if actor_id is None:
            return ()
        return tuple(_command_sort_key(actor_id, operation, receipt.provider_id)
                     for operation in ("create_hold", "approve", "decline", "cancel"))

    def claim_processing(self, receipt: InboundReceipt, token: str,
                         now: datetime, lease_until: datetime) -> bool:
        if not token or now.tzinfo is None or lease_until <= now:
            raise ValueError("Receipt lease is invalid")
        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(receipt.business_id, f"SMS#{receipt.provider_id}"),
                UpdateExpression="SET processing_token = :token, "
                                 "processing_lease_until = :until",
                ConditionExpression="attribute_exists(PK) AND "
                                    "authorized_for_commands = :authorized AND "
                                    "attribute_not_exists(processed_at) AND "
                                    "(attribute_not_exists(processing_lease_until) OR "
                                    "processing_lease_until < :now)",
                ExpressionAttributeValues={
                    ":token": {"S": token}, ":until": {"S": _instant(lease_until)},
                    ":now": {"S": _instant(now)}, ":authorized": {"BOOL": True},
                },
            )
        except Exception:
            current = self._get(receipt.business_id, f"SMS#{receipt.provider_id}")
            if current is not None and "processed_at" in current:
                return False
            raise
        return True

    def mark_processed(self, receipt: InboundReceipt, token: str, now: datetime) -> None:
        self._client.update_item(
            TableName=self._table,
            Key=self._key(receipt.business_id, f"SMS#{receipt.provider_id}"),
            UpdateExpression="SET processed_at = :now "
                             "REMOVE processing_token, processing_lease_until",
            ConditionExpression="processing_token = :token AND "
                                "attribute_not_exists(processed_at)",
            ExpressionAttributeValues={
                ":token": {"S": token}, ":now": {"S": _instant(now)},
            },
        )

    def put_reply(self, receipt: InboundReceipt, text: str,
                  token: str, now: datetime) -> bool:
        """Atomically persist one trusted reply and its delivery intent."""
        if (not receipt.authorized_for_commands or receipt.body is None
                or receipt.keyword != Keyword.OTHER or now.tzinfo is None
                or not text or len(text) > 500 or not token):
            raise ValueError("Conversation reply is not eligible")
        outbox_id = f"sms-reply#{receipt.provider_id}"
        instant = _instant(now)
        recipient = receipt.role.value
        if recipient not in {"owner", "client"}:
            raise ValueError("Conversation recipient is unknown")
        outbox = {
            **self._key(receipt.business_id, f"OUTBOX#{outbox_id}"),
            **due_keys(DeliveryState.PENDING, now, outbox_id),
            "outbox_id": {"S": outbox_id},
            "entity_id": {"S": receipt.provider_id},
            "recipient": {"S": recipient},
            "template": {"S": "conversation-reply"},
            "delivery_state": {"S": "PENDING"},
            "created_at": {"S": instant},
            "next_attempt_at": {"S": instant},
            "dispatch_after": {"S": instant},
            "attempts": {"N": "0"},
            "event_version": {"N": "0"},
        }
        try:
            self._client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": self._table,
                    "Key": self._key(receipt.business_id, f"SMS#{receipt.provider_id}"),
                    "UpdateExpression": "SET reply_text = :text, processed_at = :now "
                                        "REMOVE processing_token, processing_lease_until",
                    "ConditionExpression": "attribute_exists(PK) AND "
                                           "attribute_not_exists(reply_text) AND "
                                           "attribute_not_exists(processed_at) AND "
                                           "processing_token = :token AND "
                                           "authorized_for_commands = :authorized",
                    "ExpressionAttributeValues": {
                        ":text": {"S": text}, ":now": {"S": instant},
                        ":token": {"S": token}, ":authorized": {"BOOL": True},
                    },
                }},
                {"Put": {
                    "TableName": self._table, "Item": outbox,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
                {"ConditionCheck": {
                    "TableName": self._table,
                    "Key": self._key(receipt.business_id, f"SMS_SUPPRESS#{receipt.sender}"),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
                {"ConditionCheck": {
                    "TableName": self._table,
                    "Key": self._key(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}"),
                    "ConditionExpression": "attribute_not_exists(PK) OR attribute_exists(cleared_at)",
                }},
                *({"ConditionCheck": {
                    "TableName": self._table,
                    "Key": self._key(receipt.business_id, key),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }} for key in self._command_keys(receipt)),
                *([self._erasure_check(receipt.business_id, receipt.client_id)]
                  if receipt.client_id is not None else []),
                self._phone_erasure_check(receipt.business_id, receipt.sender),
            ])
        except Exception as exc:
            if self.read_reply_text(receipt.business_id, receipt.provider_id) is not None:
                return False
            response = getattr(exc, "response", {})
            if (isinstance(response, dict) and response.get("Error", {}).get("Code")
                    == "TransactionCanceledException"):
                # A STOP or command may have won this transaction. Do not
                # replay the old text after either state changes again.
                raise SmsCommandInterrupted from exc
            raise
        return True

    def put_received(self, receipt: InboundReceipt) -> bool:
        item: dict[str, Any] = {
            **self._key(receipt.business_id, f"SMS#{receipt.provider_id}"),
            "provider_id": {"S": receipt.provider_id},
            "sender": {"S": receipt.sender},
            "recipient": {"S": receipt.recipient},
            "received_at": {"S": _instant(receipt.received_at)},
            "role": {"S": receipt.role.value},
            "keyword": {"S": receipt.keyword.value},
            "authorized_for_commands": {"BOOL": receipt.authorized_for_commands},
        }
        if receipt.body is not None:
            item["body"] = {"S": receipt.body}
        if receipt.client_id is not None:
            item["client_id"] = {"S": receipt.client_id}
        writes: list[dict[str, Any]] = [{"Put": {
            "TableName": self._table, "Item": item,
            "ConditionExpression": "attribute_not_exists(PK)",
        }}]
        if receipt.client_id is not None:
            writes.append(self._erasure_check(receipt.business_id, receipt.client_id))
        writes.append(self._phone_erasure_check(receipt.business_id, receipt.sender))
        if receipt.keyword == Keyword.STOP:
            writes.append({"Put": {
                "TableName": self._table,
                "Item": {**self._key(receipt.business_id,
                                      f"SMS_OPTOUT_EVENT#{receipt.sender}#{receipt.provider_id}"),
                         "phone_e164": {"S": receipt.sender},
                         "opted_out_at": {"S": _instant(receipt.received_at)}},
                "ConditionExpression": "attribute_not_exists(PK)",
            }})
            current_stop = self._get(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}")
            if current_stop is None or "legal_hold_reason" not in current_stop:
                writes.append({"Update": {
                    "TableName": self._table,
                    "Key": self._key(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}"),
                    "UpdateExpression": "SET opted_out_at = :at, phone_e164 = :phone "
                                        "REMOVE cleared_at",
                    "ConditionExpression": "attribute_not_exists(legal_hold_reason)",
                    "ExpressionAttributeValues": {
                        ":at": {"S": _instant(receipt.received_at)},
                        ":phone": {"S": receipt.sender},
                    },
                }})
            writes.append({"Put": {
                "TableName": self._table,
                "Item": {**self._key(receipt.business_id, f"SMS_SUPPRESS#{receipt.sender}"),
                         "phone_e164": {"S": receipt.sender},
                         "suppressed_at": {"S": _instant(receipt.received_at)},
                         "suppressed": {"BOOL": True}},
            }})
        if receipt.keyword == Keyword.START:
            optout = self._get(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}")
            suppress = self._get(receipt.business_id, f"SMS_SUPPRESS#{receipt.sender}")
            consent = self.read_consent(receipt.business_id, receipt.sender)
            stopped_at = (suppress["suppressed_at"]["S"] if suppress is not None else
                          optout["opted_out_at"]["S"] if optout is not None else None)
            if (stopped_at is not None and consent is not None and
                    consent.agreed_at > datetime.fromisoformat(stopped_at)):
                if optout is not None:
                    writes.append({"Update" if "legal_hold_reason" in optout else "Delete": {
                        "TableName": self._table,
                        "Key": self._key(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}"),
                        "ConditionExpression": ("opted_out_at = :at AND " +
                            ("attribute_exists(legal_hold_reason)" if "legal_hold_reason" in optout
                             else "attribute_not_exists(legal_hold_reason)")),
                        "ExpressionAttributeValues": {
                            ":at": optout["opted_out_at"],
                            ":cleared": {"S": _instant(receipt.received_at)},
                        } if "legal_hold_reason" in optout else {":at": optout["opted_out_at"]},
                        **({"UpdateExpression": "SET cleared_at = :cleared"}
                           if "legal_hold_reason" in optout else {}),
                    }})
                if suppress is not None:
                    writes.append({"Delete": {
                        "TableName": self._table,
                        "Key": self._key(receipt.business_id, f"SMS_SUPPRESS#{receipt.sender}"),
                        "ConditionExpression": "suppressed_at = :at",
                        "ExpressionAttributeValues": {":at": suppress["suppressed_at"]},
                    }})
        thread_key = f"SMS_THREAD#{receipt.sender}"
        for _ in range(3):
            attempt = list(writes)
            tracks_program = receipt.role != SenderRole.UNKNOWN or receipt.keyword == Keyword.STOP
            if tracks_program:
                thread = self._get(receipt.business_id, thread_key)
                at = _instant(receipt.received_at)
                fields = ["last_program_text_at"]
                if receipt.body is not None:
                    fields.append("last_exchange_at")
                fields = [field for field in fields if thread is None or
                          thread.get(field, {}).get("S", "") < at]
                if fields:
                    attempt.append({"Update": {
                        "TableName": self._table,
                        "Key": self._key(receipt.business_id, thread_key),
                        "UpdateExpression": "SET " + ", ".join(f"{field} = :at" for field in fields),
                        "ConditionExpression": " AND ".join(
                            f"(attribute_not_exists({field}) OR {field} <= :at)"
                            for field in fields),
                        "ExpressionAttributeValues": {":at": {"S": at}},
                    }})
            try:
                self._client.transact_write_items(TransactItems=attempt)
                return True
            except Exception as exc:
                # A duplicate provider retry is harmless. A racing newer message
                # can change the thread; reread it before retrying. Other errors
                # propagate so Twilio retries rather than dropping a STOP.
                if self._get(receipt.business_id, f"SMS#{receipt.provider_id}") is not None:
                    return False
                response = getattr(exc, "response", {})
                conflict = (isinstance(response, dict) and response.get("Error", {}).get("Code")
                            == "TransactionCanceledException")
                if conflict and receipt.keyword == Keyword.STOP:
                    current = self._get(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}")
                    if current is not None and "legal_hold_reason" in current:
                        writes = [action for action in writes if not (
                            "Update" in action and action["Update"]["Key"]["SK"]["S"]
                            == f"SMS_OPTOUT#{receipt.sender}")]
                if conflict and receipt.keyword == Keyword.START:
                    current = self._get(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}")
                    if current is not None and "legal_hold_reason" in current:
                        for action in writes:
                            if ("Delete" in action and action["Delete"]["Key"]["SK"]["S"]
                                    == f"SMS_OPTOUT#{receipt.sender}"):
                                action.pop("Delete")
                                action["Update"] = {
                                    "TableName": self._table,
                                    "Key": self._key(receipt.business_id,
                                                     f"SMS_OPTOUT#{receipt.sender}"),
                                    "UpdateExpression": "SET cleared_at = :cleared",
                                    "ConditionExpression": ("opted_out_at = :at AND "
                                                            "attribute_exists(legal_hold_reason)"),
                                    "ExpressionAttributeValues": {
                                        ":at": current["opted_out_at"],
                                        ":cleared": {"S": _instant(receipt.received_at)},
                                    },
                                }
                if not conflict and len(attempt) == len(writes):
                    raise
        raise RuntimeError("SMS thread changed during every receipt attempt")

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        marker = self._get(business_id, f"SMS_SUPPRESS#{phone_e164}")
        legacy = self._get(business_id, f"SMS_OPTOUT#{phone_e164}")
        return marker is not None or (legacy is not None and "cleared_at" not in legacy)

    def record_outbound(self, business_id: str, phone_e164: str,
                        provider_id: str, sent_at: datetime) -> None:
        """Advance the retention clock after provider acceptance, without storing the body."""
        phone = normalize_phone(phone_e164)
        if not provider_id or sent_at.tzinfo is None:
            raise ValueError("Outbound evidence needs a provider ID and aware timestamp")
        receipt_key = f"SMS_OUT#{provider_id}"
        thread_key = f"SMS_THREAD#{phone}"
        sent = _instant(sent_at)
        outbound = {
            **self._key(business_id, receipt_key),
            "provider_id": {"S": provider_id},
            "recipient": {"S": phone},
            "sent_at": {"S": sent},
        }
        for _ in range(3):
            writes: list[dict[str, Any]] = [{"Put": {
                "TableName": self._table, "Item": outbound,
                "ConditionExpression": "attribute_not_exists(PK)",
            }}]
            thread = self._get(business_id, thread_key)
            fields = [field for field in ("last_exchange_at", "last_program_text_at")
                      if thread is None or thread.get(field, {}).get("S", "") < sent]
            if fields:
                writes.append({"Update": {
                    "TableName": self._table,
                    "Key": self._key(business_id, thread_key),
                    "UpdateExpression": "SET " + ", ".join(f"{field} = :at" for field in fields),
                    "ConditionExpression": " AND ".join(
                        f"(attribute_not_exists({field}) OR {field} <= :at)"
                        for field in fields),
                    "ExpressionAttributeValues": {":at": {"S": sent}},
                }})
            try:
                self._client.transact_write_items(TransactItems=writes)
                return
            except Exception:
                if self._get(business_id, receipt_key) is not None:
                    return
                if len(writes) == 1:
                    raise
        raise RuntimeError("SMS thread changed during every outbound attempt")

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        item = self._get(business_id, f"SMS_CONSENT_CURRENT#{phone_e164}")
        if item is None:
            return None
        return ConsentEvidence(
            business_id, item["client_id"]["S"], item["participant_name"]["S"],
            phone_e164, datetime.fromisoformat(item["agreed_at"]["S"]),
            item["script_version"]["S"], item["method"]["S"],
        )

    def _consent_writes(self, evidence: ConsentEvidence) -> list[dict[str, Any]]:
        attrs = {
            "client_id": {"S": evidence.client_id},
            "participant_name": {"S": evidence.participant_name},
            "phone_e164": {"S": evidence.phone_e164},
            "agreed_at": {"S": _instant(evidence.agreed_at)},
            "script_version": {"S": evidence.script_version},
            "method": {"S": evidence.method},
            "response": {"S": "yes"},
        }
        history_key = f"SMS_CONSENT#{evidence.phone_e164}#{_instant(evidence.agreed_at)}"
        return [
            {"Put": {"TableName": self._table,
                     "Item": {**self._key(evidence.business_id, history_key), **attrs},
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Update": {"TableName": self._table,
                        "Key": self._key(evidence.business_id,
                                         f"SMS_CONSENT_CURRENT#{evidence.phone_e164}"),
                        "UpdateExpression": "SET " + ", ".join(
                            f"#{key} = :{key}" for key in attrs),
                        "ExpressionAttributeNames": {f"#{key}": key for key in attrs},
                        "ExpressionAttributeValues": {f":{key}": value
                                                      for key, value in attrs.items()}}},
        ]

    def _client_has_consent(self, business_id: str, client_id: str) -> bool:
        """Any permanent consent history record for this client, under any phone.

        Uses the append-only SMS_CONSENT# history rather than SMS_CONSENT_CURRENT#,
        which is keyed by phone and is overwritten when another client consents
        on the same number.
        """
        start: dict[str, Any] | None = None
        while True:
            arguments: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                "FilterExpression": "client_id = :client",
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"BUSINESS#{business_id}"},
                    ":prefix": {"S": "SMS_CONSENT#"},
                    ":client": {"S": client_id},
                },
                "ConsistentRead": True,
            }
            if start is not None:
                arguments["ExclusiveStartKey"] = start
            page = self._client.query(**arguments)
            if page.get("Items"):
                return True
            start = page.get("LastEvaluatedKey")
            if start is None:
                return False

    def put_consent(self, evidence: ConsentEvidence) -> None:
        self._client.transact_write_items(TransactItems=[
            *self._consent_writes(evidence),
            self._erasure_check(evidence.business_id, evidence.client_id),
            self._phone_erasure_check(evidence.business_id, evidence.phone_e164),
        ])

    def put_consent_verifying_phone(self, evidence: ConsentEvidence,
                                    verified: ClientProfile,
                                    welcome: OutboxRecord | None = None) -> None:
        """One transaction: consent records, the profile's verification, optional welcome.

        A welcome is dropped when its outbox ID already exists or when any earlier
        consent record names this client (a client enrolled before welcome texts
        existed), so only a first enrollment is welcomed. The conditional Put and
        the profile version check close the race.
        """
        if (verified.phone_verified_at is None or verified.version < 2
                or verified.business_id != evidence.business_id
                or verified.client_id != evidence.client_id
                or verified.phone_e164 != evidence.phone_e164):
            raise ValueError("Verified profile must match the consent evidence")
        profile_update = {"Update": {
            "TableName": self._table,
            "Key": self._key(verified.business_id, f"CLIENT#{verified.client_id}"),
            "UpdateExpression": ("SET phone_verified_at = :verified, updated_at = :updated, "
                                 "version = :new_version"),
            "ConditionExpression": ("attribute_exists(PK) AND version = :old_version "
                                    "AND phone_e164 = :phone AND active = :active"),
            "ExpressionAttributeValues": {
                ":verified": {"S": _instant(verified.phone_verified_at)},
                ":updated": {"S": _instant(verified.updated_at)},
                ":new_version": {"N": str(verified.version)},
                ":old_version": {"N": str(verified.version - 1)},
                ":phone": {"S": verified.phone_e164},
                ":active": {"BOOL": True},
            },
        }}
        writes = [*self._consent_writes(evidence), profile_update]
        if welcome is not None and (
                welcome.business_id != evidence.business_id
                or welcome.entity_id != evidence.client_id):
            raise ValueError("Welcome must belong to the consenting client")
        if (welcome is not None
                and self._get(welcome.business_id, f"OUTBOX#{welcome.outbox_id}") is None
                and not self._client_has_consent(welcome.business_id, welcome.entity_id)):
            instant = _instant(welcome.created_at)
            writes.append({"Put": {
                "TableName": self._table,
                "Item": {
                    **self._key(welcome.business_id, f"OUTBOX#{welcome.outbox_id}"),
                    **due_keys(DeliveryState.PENDING, welcome.created_at, welcome.outbox_id),
                    "outbox_id": {"S": welcome.outbox_id},
                    "entity_id": {"S": welcome.entity_id},
                    "recipient": {"S": welcome.recipient},
                    "template": {"S": welcome.template},
                    "delivery_state": {"S": "PENDING"},
                    "created_at": {"S": instant},
                    "next_attempt_at": {"S": instant},
                    "dispatch_after": {"S": instant},
                    "attempts": {"N": "0"},
                    "event_version": {"N": str(welcome.event_version)},
                },
                "ConditionExpression": "attribute_not_exists(PK)",
            }})
        writes.extend([
            self._erasure_check(evidence.business_id, evidence.client_id),
            self._phone_erasure_check(evidence.business_id, evidence.phone_e164),
        ])
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as exc:
            if not _record_transaction_conflict(exc):
                raise
            raise RecordConflict("Client profile changed; nothing was recorded") from exc

    def set_evidence_legal_hold(self, business_id: str, sort_key: str,
                                reason: str | None) -> None:
        """Trusted admin operation; callers must authorize and document the case."""
        if not sort_key.startswith(("SMS_CONSENT#", "SMS_CONSENT_CURRENT#", "SMS_OPTOUT#",
                                    "SMS_OPTOUT_EVENT#")):
            raise ValueError("Only SMS evidence records can be held")
        if reason is not None and (not reason.strip() or len(reason) > 500):
            raise ValueError("Legal hold needs a documented reason under 500 characters")
        values = ({":reason": {"S": reason.strip()}} if reason is not None else {})
        arguments: dict[str, Any] = {
            "TableName": self._table,
            "Key": self._key(business_id, sort_key),
            "ConditionExpression": "attribute_exists(PK)",
            "UpdateExpression": ("SET legal_hold_reason = :reason" if reason is not None
                                 else "REMOVE legal_hold_reason"),
        }
        if values:
            arguments["ExpressionAttributeValues"] = values
        self._client.update_item(**arguments)

    def put_status(self, status: SmsDeliveryStatus) -> None:
        values: dict[str, Any] = {
            ":rank": {"N": str(STATUS_RANK[status.status])},
            ":status": {"S": status.status},
            ":recipient": {"S": status.recipient},
            ":at": {"S": _instant(status.observed_at)},
            ":outbox": {"S": status.outbox_id},
            ":provider": {"S": status.provider_id},
        }
        expression = ("SET status_rank = :rank, delivery_status = :status, "
                      "recipient = :recipient, observed_at = :at, "
                      "outbox_id = :outbox, provider_id = :provider")
        if status.error_code is not None:
            expression += ", error_code = :error"
            values[":error"] = {"S": status.error_code}
        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(status.business_id, f"SMS_STATUS#{status.provider_id}"),
                UpdateExpression=expression,
                ConditionExpression=("attribute_not_exists(status_rank) OR "
                                     "status_rank < :rank OR "
                                     "(status_rank = :rank AND delivery_status = :status)"),
                ExpressionAttributeValues=values,
            )
        except Exception as exc:
            response = getattr(exc, "response", {})
            if isinstance(response, dict) and response.get("Error", {}).get("Code") == (
                "ConditionalCheckFailedException"
            ):
                return  # Stale or conflicting callback; first terminal result wins.
            raise

    def list_delivery_failures(self, business_id: str) -> tuple[SmsDeliveryStatus, ...]:
        """Owner follow-up view; a callback never blindly resends an accepted SMS."""
        failures: list[SmsDeliveryStatus] = []
        start: dict[str, Any] | None = None
        while True:
            arguments: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"BUSINESS#{business_id}"},
                    ":prefix": {"S": "SMS_STATUS#"},
                },
                "ConsistentRead": True,
            }
            if start is not None:
                arguments["ExclusiveStartKey"] = start
            page = self._client.query(**arguments)
            for item in page.get("Items", ()):
                status = item["delivery_status"]["S"]
                if status in {"undelivered", "failed"}:
                    failures.append(SmsDeliveryStatus(
                        business_id, item["outbox_id"]["S"], item["provider_id"]["S"],
                        status, item["recipient"]["S"],
                        datetime.fromisoformat(item["observed_at"]["S"]),
                        item["error_code"]["S"] if "error_code" in item else None,
                    ))
            start = page.get("LastEvaluatedKey")
            if start is None:
                return tuple(failures)

    def purge_expired_evidence(self, business_id: str, now: datetime) -> int:
        """Delete four-year-old consent/STOP evidence; keep active suppression state."""
        cutoff = _four_year_cutoff(now)
        removed = 0
        for prefix in ("SMS_CONSENT#", "SMS_CONSENT_CURRENT#", "SMS_OPTOUT#",
                       "SMS_OPTOUT_EVENT#"):
            start: dict[str, Any] | None = None
            while True:
                arguments: dict[str, Any] = {
                    "TableName": self._table,
                    "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                    "ExpressionAttributeValues": {
                        ":pk": {"S": f"BUSINESS#{business_id}"},
                        ":prefix": {"S": prefix},
                    },
                    "ConsistentRead": True,
                }
                if start is not None:
                    arguments["ExclusiveStartKey"] = start
                page = self._client.query(**arguments)
                for item in page.get("Items", ()):
                    if "legal_hold_reason" in item:
                        continue
                    phone = item["phone_e164"]["S"]
                    field = ("opted_out_at" if prefix.startswith("SMS_OPTOUT") else
                             "agreed_at")
                    recorded = item[field]["S"]
                    if recorded > cutoff:
                        continue
                    thread_key = f"SMS_THREAD#{phone}"
                    thread = self._get(business_id, thread_key)
                    last_text = (thread.get("last_program_text_at", thread.get("last_exchange_at", {}))
                                 .get("S", "") if thread is not None else "")
                    if last_text > cutoff:
                        continue
                    values = {":cutoff": {"S": cutoff}, ":recorded": item[field]}
                    actions: list[dict[str, Any]] = []
                    if (prefix == "SMS_OPTOUT#" and "cleared_at" not in item and
                            self._get(business_id, f"SMS_SUPPRESS#{phone}") is None):
                        # Legacy STOP rows predate the separate active marker.
                        # Migrate suppression atomically with evidence deletion.
                        actions.append({"Put": {
                            "TableName": self._table,
                            "Item": {**self._key(business_id, f"SMS_SUPPRESS#{phone}"),
                                     "phone_e164": {"S": phone},
                                     "suppressed_at": item["opted_out_at"],
                                     "suppressed": {"BOOL": True}},
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }})
                    actions.extend([
                        {"ConditionCheck": {
                            "TableName": self._table,
                            "Key": self._key(business_id, thread_key),
                            "ConditionExpression": (
                                "last_program_text_at <= :cutoff OR "
                                "(attribute_not_exists(last_program_text_at) AND "
                                "(attribute_not_exists(last_exchange_at) OR "
                                "last_exchange_at <= :cutoff))"
                            ),
                            "ExpressionAttributeValues": {":cutoff": values[":cutoff"]},
                        }},
                        {"Delete": {
                            "TableName": self._table,
                            "Key": self._key(business_id, item["SK"]["S"]),
                            "ConditionExpression": (f"{field} = :recorded AND "
                                                    "attribute_not_exists(legal_hold_reason)"),
                            "ExpressionAttributeValues": {":recorded": values[":recorded"]},
                        }},
                    ])
                    try:
                        self._client.transact_write_items(TransactItems=actions)
                    except Exception as exc:
                        response = getattr(exc, "response", {})
                        if (isinstance(response, dict) and response.get("Error", {}).get("Code")
                                == "TransactionCanceledException"):
                            continue  # New text, replacement consent, hold, or another purge won.
                        raise
                    removed += 1
                start = page.get("LastEvaluatedKey")
                if start is None:
                    break
        return removed

    def purge_expired_bodies(self, business_id: str, now: datetime) -> int:
        """Remove only bodies, 90 days after the sender's last scheduling exchange."""
        if now.tzinfo is None:
            raise ValueError("Retention clock must be timezone-aware")
        cutoff = _instant(now - timedelta(days=90))
        start: dict[str, Any] | None = None
        removed = 0
        while True:
            arguments: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"BUSINESS#{business_id}"},
                    ":prefix": {"S": "SMS#"},
                },
                "ConsistentRead": True,
            }
            if start is not None:
                arguments["ExclusiveStartKey"] = start
            page = self._client.query(**arguments)
            for item in page.get("Items", ()):
                if ("body" not in item and "reply_text" not in item) or "legal_hold_reason" in item:
                    continue
                sender = item["sender"]["S"]
                thread_key = f"SMS_THREAD#{sender}"
                thread = self._get(business_id, thread_key)
                if thread is None or thread["last_exchange_at"]["S"] > cutoff:
                    continue
                try:
                    self._client.transact_write_items(TransactItems=[
                        {"ConditionCheck": {
                            "TableName": self._table,
                            "Key": self._key(business_id, thread_key),
                            "ConditionExpression": "last_exchange_at <= :cutoff",
                            "ExpressionAttributeValues": {":cutoff": {"S": cutoff}},
                        }},
                        {"Update": {
                            "TableName": self._table,
                            "Key": self._key(business_id, item["SK"]["S"]),
                            "UpdateExpression": "REMOVE body, reply_text",
                            "ConditionExpression": "(attribute_exists(body) OR "
                                                   "attribute_exists(reply_text)) AND "
                                                   "received_at = :received",
                            "ExpressionAttributeValues": {
                                ":received": item["received_at"],
                            },
                        }},
                    ])
                except Exception as exc:
                    response = getattr(exc, "response", {})
                    if (isinstance(response, dict) and response.get("Error", {}).get("Code")
                            == "TransactionCanceledException"):
                        continue  # Another receipt or purge won; recheck on next run.
                    raise
                removed += 1
            start = page.get("LastEvaluatedKey")
            if start is None:
                return removed
