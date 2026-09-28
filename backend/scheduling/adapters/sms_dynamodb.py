"""Conditional SMS ingress and consent records in the business table."""

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SmsIngressStore
from scheduling.domain.sms_status import STATUS_RANK, SmsDeliveryStatus


class SmsDynamoClient(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]: ...
    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def query(self, **kwargs: Any) -> dict[str, Any]: ...


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


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
        if receipt.keyword == Keyword.STOP:
            writes.append({"Update": {
                "TableName": self._table,
                "Key": self._key(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}"),
                "UpdateExpression": "SET opted_out_at = :at, phone_e164 = :phone",
                "ExpressionAttributeValues": {
                    ":at": {"S": _instant(receipt.received_at)},
                    ":phone": {"S": receipt.sender},
                },
            }})
        if receipt.keyword == Keyword.START:
            optout = self._get(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}")
            consent = self.read_consent(receipt.business_id, receipt.sender)
            if (optout is not None and consent is not None and
                    consent.agreed_at > datetime.fromisoformat(optout["opted_out_at"]["S"])):
                writes.append({"Delete": {
                    "TableName": self._table,
                    "Key": self._key(receipt.business_id, f"SMS_OPTOUT#{receipt.sender}"),
                    "ConditionExpression": "opted_out_at = :at",
                    "ExpressionAttributeValues": {":at": optout["opted_out_at"]},
                }})
        thread_key = f"SMS_THREAD#{receipt.sender}"
        for _ in range(3):
            attempt = list(writes)
            if receipt.body is not None:
                thread = self._get(receipt.business_id, thread_key)
                if (thread is None or
                        thread.get("last_exchange_at", {}).get("S", "") < _instant(receipt.received_at)):
                    attempt.append({"Update": {
                        "TableName": self._table,
                        "Key": self._key(receipt.business_id, thread_key),
                        "UpdateExpression": "SET last_exchange_at = :at",
                        "ConditionExpression": (
                            "attribute_not_exists(last_exchange_at) OR last_exchange_at <= :at"
                        ),
                        "ExpressionAttributeValues": {":at": {"S": _instant(receipt.received_at)}},
                    }})
            try:
                self._client.transact_write_items(TransactItems=attempt)
                return True
            except Exception:
                # A duplicate provider retry is harmless. A racing newer message
                # can change the thread; reread it before retrying. Other errors
                # propagate so Twilio retries rather than dropping a STOP.
                if self._get(receipt.business_id, f"SMS#{receipt.provider_id}") is not None:
                    return False
                if receipt.body is None or len(attempt) == len(writes):
                    raise
        raise RuntimeError("SMS thread changed during every receipt attempt")

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return self._get(business_id, f"SMS_OPTOUT#{phone_e164}") is not None

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        item = self._get(business_id, f"SMS_CONSENT_CURRENT#{phone_e164}")
        if item is None:
            return None
        return ConsentEvidence(
            business_id, item["client_id"]["S"], item["participant_name"]["S"],
            phone_e164, datetime.fromisoformat(item["agreed_at"]["S"]),
            item["script_version"]["S"], item["method"]["S"],
        )

    def put_consent(self, evidence: ConsentEvidence) -> None:
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
        self._client.transact_write_items(TransactItems=[
            {"Put": {"TableName": self._table,
                     "Item": {**self._key(evidence.business_id, history_key), **attrs},
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": self._table,
                     "Item": {**self._key(evidence.business_id,
                                         f"SMS_CONSENT_CURRENT#{evidence.phone_e164}"), **attrs}}},
        ])

    def put_status(self, status: SmsDeliveryStatus) -> None:
        values: dict[str, Any] = {
            ":rank": {"N": str(STATUS_RANK[status.status])},
            ":status": {"S": status.status},
            ":recipient": {"S": status.recipient},
            ":at": {"S": _instant(status.observed_at)},
        }
        expression = ("SET status_rank = :rank, delivery_status = :status, "
                      "recipient = :recipient, observed_at = :at")
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
                if "body" not in item or "legal_hold_reason" in item:
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
                            "UpdateExpression": "REMOVE body",
                            "ConditionExpression": "attribute_exists(body) AND received_at = :received",
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
