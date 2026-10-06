"""Durable per-sender offer and confirmation memory in the business table.

One item per sender holds only offered start instants, one appointment ID, or
the last calendar question's day range and statuses, never message text or
visit details. Reads enforce expiry; DynamoDB TTL on ``expires_at_epoch``
removes stale items later. A clear is conditional on the state ID so an older
reply cannot erase a newer offer.
"""

from datetime import date, datetime
from hashlib import sha256
from typing import Any, Protocol

from scheduling.domain.conversation_state import ConversationState, PromptKind


class StateDynamoClient(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def put_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def delete_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]: ...


class DynamoConversationStates:
    def __init__(self, client: StateDynamoClient, table_name: str) -> None:
        self._client = client
        self._table = table_name

    @staticmethod
    def _key(business_id: str, sender: str) -> dict[str, Any]:
        return {"PK": {"S": f"BUSINESS#{business_id}"}, "SK": {"S": f"SMS_STATE#{sender}"}}

    def read_state(self, business_id: str, sender: str) -> ConversationState | None:
        item = self._client.get_item(TableName=self._table, Key=self._key(business_id, sender),
                                     ConsistentRead=True).get("Item")
        if item is None:
            return None
        return ConversationState(
            business_id, sender, item["state_id"]["S"], PromptKind(item["kind"]["S"]),
            datetime.fromisoformat(item["created_at"]["S"]),
            datetime.fromisoformat(item["expires_at"]["S"]),
            tuple(datetime.fromisoformat(value["S"])
                  for value in item.get("options", {}).get("L", ())),
            item.get("appointment_id", {}).get("S"),
            int(item["appointment_version"]["N"]) if "appointment_version" in item else None,
            item.get("client_id", {}).get("S"),
            date.fromisoformat(item["calendar_first"]["S"]) if "calendar_first" in item else None,
            date.fromisoformat(item["calendar_last"]["S"]) if "calendar_last" in item else None,
            tuple(value["S"] for value in item.get("calendar_statuses", {}).get("L", ())),
            item.get("calendar_view", {}).get("S"),
        )

    def put_state(self, state: ConversationState) -> None:
        item: dict[str, Any] = {
            **self._key(state.business_id, state.sender),
            "state_id": {"S": state.state_id},
            "kind": {"S": state.kind.value},
            "created_at": {"S": state.created_at.isoformat()},
            "expires_at": {"S": state.expires_at.isoformat()},
            "expires_at_epoch": {"N": str(int(state.expires_at.timestamp()))},
        }
        if state.options:
            item["options"] = {"L": [{"S": option.isoformat()} for option in state.options]}
        if state.appointment_id is not None:
            item["appointment_id"] = {"S": state.appointment_id}
        if state.appointment_version is not None:
            item["appointment_version"] = {"N": str(state.appointment_version)}
        if state.client_id is not None:
            item["client_id"] = {"S": state.client_id}
        if state.calendar_first is not None and state.calendar_last is not None:
            item["calendar_first"] = {"S": state.calendar_first.isoformat()}
            item["calendar_last"] = {"S": state.calendar_last.isoformat()}
        if state.calendar_statuses:
            item["calendar_statuses"] = {"L": [{"S": value} for value in state.calendar_statuses]}
        if state.calendar_view is not None:
            item["calendar_view"] = {"S": state.calendar_view}
        checks: list[dict[str, Any]] = []
        if state.client_id is not None:
            checks.append({"ConditionCheck": {
                "TableName": self._table,
                "Key": {"PK": {"S": f"BUSINESS#{state.business_id}"},
                        "SK": {"S": f"ERASURE#{sha256(state.client_id.encode()).hexdigest()}"}},
                "ConditionExpression": "attribute_not_exists(PK)",
            }})
        checks.append({"ConditionCheck": {
            "TableName": self._table,
            "Key": {"PK": {"S": f"BUSINESS#{state.business_id}"},
                    "SK": {"S": f"ERASURE_PHONE#{state.sender}"}},
            "ConditionExpression": "attribute_not_exists(PK)",
        }})
        self._client.transact_write_items(TransactItems=[
            {"Put": {"TableName": self._table, "Item": item}},
            *checks,
        ])

    def clear_state(self, business_id: str, sender: str, state_id: str) -> None:
        try:
            self._client.delete_item(
                TableName=self._table, Key=self._key(business_id, sender),
                ConditionExpression="state_id = :id",
                ExpressionAttributeValues={":id": {"S": state_id}})
        except Exception as exc:
            response = getattr(exc, "response", {})
            if (isinstance(response, dict) and response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"):
                return  # A newer prompt replaced this one, or it is already gone.
            raise
