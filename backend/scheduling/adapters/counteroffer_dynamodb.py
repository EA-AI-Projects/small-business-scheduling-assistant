"""Durable owner counteroffers in the business table.

Items (all carry ``client_id`` so owner-requested client deletion removes them):
``COUNTEROFFER#<offer>`` the versioned offer, ``COUNTEROFFER_ACTIVE#<owner>`` the
owner's current offer ID, and ``COUNTEROFFER_CLIENT#<client>`` the client's latest
confirmed offer ID for the acceptance stage (#176). Confirmation is one
transaction: the version-guarded state change, the single client outbox intent,
and the client pointer commit together or not at all.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol

from scheduling.adapters.dynamodb import _record_transaction_conflict
from scheduling.adapters.outbox_aws import due_keys
from scheduling.domain.outbox import DeliveryState, OutboxRecord
from scheduling.domain.owner_counteroffer import Counteroffer, OfferState, confirmed_offer

RETAIN_AFTER_EXPIRY = timedelta(days=1)


class CounterofferDynamoClient(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]: ...
    def delete_item(self, **kwargs: Any) -> dict[str, Any]: ...


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class DynamoCounterofferStore:
    def __init__(self, client: CounterofferDynamoClient, table_name: str) -> None:
        self._client = client
        self._table = table_name

    @staticmethod
    def _key(business_id: str, sort_key: str) -> dict[str, Any]:
        return {"PK": {"S": f"BUSINESS#{business_id}"}, "SK": {"S": sort_key}}

    def _get(self, business_id: str, sort_key: str) -> dict[str, Any] | None:
        return self._client.get_item(TableName=self._table,
                                     Key=self._key(business_id, sort_key),
                                     ConsistentRead=True).get("Item")

    def _erasure_checks(self, offer: Counteroffer) -> list[dict[str, Any]]:
        def absent(sort_key: str) -> dict[str, Any]:
            return {"ConditionCheck": {
                "TableName": self._table, "Key": self._key(offer.business_id, sort_key),
                "ConditionExpression": "attribute_not_exists(PK)"}}
        return [absent(f"ERASURE#{sha256(offer.client_id.encode()).hexdigest()}"),
                absent(f"ERASURE_PHONE#{offer.client_phone}")]

    @staticmethod
    def _offer(item: dict[str, Any]) -> Counteroffer:
        return Counteroffer(
            item["business_id"]["S"], item["offer_id"]["S"], item["owner"]["S"],
            item["request_id"]["S"], int(item["request_version"]["N"]), item["client_id"]["S"],
            item["client_phone"]["S"], datetime.fromisoformat(item["proposed_start"]["S"]),
            int(item["duration_minutes"]["N"]), item["text"]["S"],
            OfferState(item["state"]["S"]), int(item["version"]["N"]),
            datetime.fromisoformat(item["created_at"]["S"]),
            datetime.fromisoformat(item["expires_at"]["S"]),
            datetime.fromisoformat(item["confirmed_at"]["S"]) if "confirmed_at" in item else None,
            item.get("confirmed_by", {}).get("S"), item.get("failure", {}).get("S"))

    def _item(self, offer: Counteroffer) -> dict[str, Any]:
        item: dict[str, Any] = {
            **self._key(offer.business_id, f"COUNTEROFFER#{offer.offer_id}"),
            "business_id": {"S": offer.business_id}, "offer_id": {"S": offer.offer_id},
            "owner": {"S": offer.owner}, "request_id": {"S": offer.request_id},
            "request_version": {"N": str(offer.request_version)},
            "client_id": {"S": offer.client_id}, "client_phone": {"S": offer.client_phone},
            "proposed_start": {"S": _instant(offer.proposed_start)},
            "duration_minutes": {"N": str(offer.duration_minutes)},
            "text": {"S": offer.text}, "state": {"S": offer.state.value},
            "version": {"N": str(offer.version)},
            "created_at": {"S": _instant(offer.created_at)},
            "expires_at": {"S": _instant(offer.expires_at)},
            "expires_at_epoch": {"N": str(int(
                (offer.expires_at + RETAIN_AFTER_EXPIRY).timestamp()))},
        }
        if offer.confirmed_at is not None:
            item["confirmed_at"] = {"S": _instant(offer.confirmed_at)}
        if offer.confirmed_by is not None:
            item["confirmed_by"] = {"S": offer.confirmed_by}
        if offer.failure is not None:
            item["failure"] = {"S": offer.failure}
        return item

    def _pointer(self, offer: Counteroffer, sort_key: str) -> dict[str, Any]:
        return {
            **self._key(offer.business_id, sort_key),
            "offer_id": {"S": offer.offer_id}, "client_id": {"S": offer.client_id},
            "expires_at_epoch": {"N": str(int(
                (offer.expires_at + RETAIN_AFTER_EXPIRY).timestamp()))},
        }

    def read(self, business_id: str, offer_id: str) -> Counteroffer | None:
        item = self._get(business_id, f"COUNTEROFFER#{offer_id}")
        return self._offer(item) if item is not None else None

    def read_active(self, business_id: str, owner: str) -> Counteroffer | None:
        pointer = self._get(business_id, f"COUNTEROFFER_ACTIVE#{owner}")
        return self.read(business_id, pointer["offer_id"]["S"]) if pointer is not None else None

    def read_confirmed_for_client(self, business_id: str,
                                  client_id: str) -> Counteroffer | None:
        pointer = self._get(business_id, f"COUNTEROFFER_CLIENT#{client_id}")
        return self.read(business_id, pointer["offer_id"]["S"]) if pointer is not None else None

    def put_draft(self, offer: Counteroffer) -> bool:
        if offer.state != OfferState.PROPOSED:
            raise ValueError("Only a proposed offer can be stored as a draft")
        try:
            self._client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self._table, "Item": self._item(offer),
                         # A redelivered owner message rewrites the same proposal.
                         "ConditionExpression": "attribute_not_exists(PK) OR #s = :proposed",
                         "ExpressionAttributeNames": {"#s": "state"},
                         "ExpressionAttributeValues": {":proposed": {"S": "PROPOSED"}}}},
                {"Put": {"TableName": self._table,
                         "Item": self._pointer(offer, f"COUNTEROFFER_ACTIVE#{offer.owner}")}},
                *self._erasure_checks(offer),
            ])
        except Exception as exc:
            if not _record_transaction_conflict(exc):
                raise
            return False  # The offer already finished, or the client was erased.
        return True

    def discard(self, offer: Counteroffer) -> None:
        try:
            self._client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": self._table,
                    "Key": self._key(offer.business_id, f"COUNTEROFFER#{offer.offer_id}"),
                    "UpdateExpression": "SET #s = :discarded, #v = :next",
                    "ConditionExpression": "#s = :proposed AND #v = :version",
                    "ExpressionAttributeNames": {"#s": "state", "#v": "version"},
                    "ExpressionAttributeValues": {
                        ":discarded": {"S": OfferState.DISCARDED.value},
                        ":proposed": {"S": OfferState.PROPOSED.value},
                        ":version": {"N": str(offer.version)},
                        ":next": {"N": str(offer.version + 1)}}}},
            ])
        except Exception as exc:
            if not _record_transaction_conflict(exc):
                raise

    def clear_active(self, business_id: str, owner: str, offer_id: str) -> None:
        try:
            self._client.delete_item(
                TableName=self._table, Key=self._key(business_id, f"COUNTEROFFER_ACTIVE#{owner}"),
                ConditionExpression="offer_id = :id",
                ExpressionAttributeValues={":id": {"S": offer_id}})
        except Exception as exc:
            response = getattr(exc, "response", {})
            if (isinstance(response, dict) and response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"):
                return  # A newer offer replaced it, or it is already gone.
            raise

    def confirm(self, offer: Counteroffer, confirmed_by: str, now: datetime,
                outbox: OutboxRecord) -> Counteroffer | None:
        if (outbox.entity_id != offer.offer_id or outbox.business_id != offer.business_id
                or outbox.recipient != "client"):
            raise ValueError("Outbox intent must belong to the confirmed offer")
        confirmed = confirmed_offer(offer, confirmed_by, now)
        instant = _instant(outbox.created_at)
        try:
            self._client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self._table, "Item": self._item(confirmed),
                         "ConditionExpression": "#s = :proposed AND #v = :version",
                         "ExpressionAttributeNames": {"#s": "state", "#v": "version"},
                         "ExpressionAttributeValues": {
                             ":proposed": {"S": OfferState.PROPOSED.value},
                             ":version": {"N": str(offer.version)}}}},
                {"Put": {"TableName": self._table, "Item": {
                    **self._key(outbox.business_id, f"OUTBOX#{outbox.outbox_id}"),
                    **due_keys(DeliveryState.PENDING, outbox.created_at, outbox.outbox_id),
                    "outbox_id": {"S": outbox.outbox_id}, "entity_id": {"S": outbox.entity_id},
                    "client_id": {"S": offer.client_id},
                    "recipient": {"S": outbox.recipient}, "template": {"S": outbox.template},
                    "delivery_state": {"S": "PENDING"}, "created_at": {"S": instant},
                    "next_attempt_at": {"S": instant}, "dispatch_after": {"S": instant},
                    "attempts": {"N": "0"}, "event_version": {"N": str(outbox.event_version)},
                }, "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": self._table,
                         "Item": self._pointer(confirmed, f"COUNTEROFFER_CLIENT#{offer.client_id}")}},
                *self._erasure_checks(offer),
            ])
        except Exception as exc:
            if not _record_transaction_conflict(exc):
                raise
            return None
        return confirmed

    def record_failure(self, offer: Counteroffer, problem: str, now: datetime,
                       outbox: OutboxRecord) -> None:
        if (outbox.entity_id != offer.offer_id or outbox.business_id != offer.business_id
                or outbox.recipient != "owner"):
            raise ValueError("Outbox intent must belong to the failed offer")
        instant = _instant(outbox.created_at)
        try:
            self._client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": self._table,
                    "Key": self._key(offer.business_id, f"COUNTEROFFER#{offer.offer_id}"),
                    "UpdateExpression": "SET failure = :problem",
                    "ConditionExpression": "attribute_exists(PK) AND attribute_not_exists(failure)",
                    "ExpressionAttributeValues": {":problem": {"S": problem}}}},
                {"Put": {"TableName": self._table, "Item": {
                    **self._key(outbox.business_id, f"OUTBOX#{outbox.outbox_id}"),
                    **due_keys(DeliveryState.PENDING, outbox.created_at, outbox.outbox_id),
                    "outbox_id": {"S": outbox.outbox_id}, "entity_id": {"S": outbox.entity_id},
                    "client_id": {"S": offer.client_id},
                    "recipient": {"S": outbox.recipient}, "template": {"S": outbox.template},
                    "delivery_state": {"S": "PENDING"}, "created_at": {"S": instant},
                    "next_attempt_at": {"S": instant}, "dispatch_after": {"S": instant},
                    "attempts": {"N": "0"}, "event_version": {"N": str(outbox.event_version)},
                }, "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
        except Exception as exc:
            if not _record_transaction_conflict(exc):
                raise  # Already recorded by an earlier attempt: nothing more to queue.
