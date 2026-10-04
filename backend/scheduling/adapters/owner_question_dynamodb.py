"""Durable memory of the owner's last calendar question, so follow-ups survive Lambda restarts.

One item per sender holds only the question type, a date range, and status names;
never message text or client data. Reads enforce expiry; DynamoDB TTL on
``expires_at_epoch`` removes stale items later.
"""

from datetime import date, datetime
from typing import Any, Protocol

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.owner_calendar_questions import Ask, QuestionContext, View


class QuestionDynamoClient(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def put_item(self, **kwargs: Any) -> dict[str, Any]: ...


class DynamoQuestionContexts:
    def __init__(self, client: QuestionDynamoClient, table_name: str) -> None:
        self._client = client
        self._table = table_name

    @staticmethod
    def _key(business_id: str, sender: str) -> dict[str, Any]:
        return {"PK": {"S": f"BUSINESS#{business_id}"}, "SK": {"S": f"OWNER_QUESTION#{sender}"}}

    def read_context(self, business_id: str, sender: str) -> QuestionContext | None:
        item = self._client.get_item(TableName=self._table, Key=self._key(business_id, sender),
                                     ConsistentRead=True).get("Item")
        if item is None:
            return None
        return QuestionContext(
            business_id, sender, View(item["view"]["S"]),
            date.fromisoformat(item["first"]["S"]) if "first" in item else None,
            date.fromisoformat(item["last"]["S"]) if "last" in item else None,
            frozenset(CalendarStatus(value) for value in item["statuses"]["SS"])
            if "statuses" in item else None,
            int(item["skip"]["N"]), Ask(item["ask"]["S"]) if "ask" in item else None,
            datetime.fromisoformat(item["created_at"]["S"]),
            datetime.fromisoformat(item["expires_at"]["S"]),
            int(item["page_start"]["N"]), item.get("receipt_id", {}).get("S", ""),
            item.get("fingerprint", {}).get("S", ""),
            item.get("clarified_request", {}).get("S", ""),
            int(item.get("clarified_version", {"N": "0"})["N"]),
            item.get("clarified_by", {}).get("S", ""),
            datetime.fromisoformat(item["clarified_at"]["S"]) if "clarified_at" in item else None)

    def put_context(self, context: QuestionContext) -> None:
        item: dict[str, Any] = {
            **self._key(context.business_id, context.sender),
            "view": {"S": context.view.value}, "skip": {"N": str(context.skip)},
            "page_start": {"N": str(context.page_start)},
            "clarified_version": {"N": str(context.clarified_version)},
            "created_at": {"S": context.created_at.isoformat()},
            "expires_at": {"S": context.expires_at.isoformat()},
            "expires_at_epoch": {"N": str(int(context.expires_at.timestamp()))},
        }
        if context.first is not None and context.last is not None:
            item["first"] = {"S": context.first.isoformat()}
            item["last"] = {"S": context.last.isoformat()}
        if context.statuses:
            item["statuses"] = {"SS": sorted(status.value for status in context.statuses)}
        for name in ("receipt_id", "fingerprint", "clarified_request", "clarified_by"):
            if getattr(context, name):
                item[name] = {"S": getattr(context, name)}
        if context.clarified_at is not None:
            item["clarified_at"] = {"S": context.clarified_at.isoformat()}
        if context.ask is not None:
            item["ask"] = {"S": context.ask.value}
        self._client.put_item(TableName=self._table, Item=item)
