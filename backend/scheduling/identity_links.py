"""Server-owned Cognito subject links and fail-closed identity resolution."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol


class LinkRole(StrEnum):
    OWNER = "owner"
    CLIENT = "client"


class LinkState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(frozen=True)
class IdentityLink:
    subject: str
    role: LinkRole
    business_id: str
    client_id: str | None
    state: LinkState
    version: int
    updated_at: datetime
    changed_by: str
    reason: str

    def __post_init__(self) -> None:
        if not self.subject or not self.business_id or not self.changed_by or not self.reason:
            raise ValueError("Subject, business, actor and reason are required")
        if self.role == LinkRole.CLIENT and not self.client_id:
            raise ValueError("A client link needs a profile")
        if self.role == LinkRole.OWNER and self.client_id is not None:
            raise ValueError("An owner link cannot name a client")


class LinkConflict(ValueError):
    """The requested link conflicts with current identity or profile state."""


class LinkStore(Protocol):
    def read_link(self, subject: str) -> IdentityLink | None: ...
    def read_target_subject(self, role: LinkRole, business_id: str,
                            client_id: str | None) -> str | None: ...
    def read_profile(self, business_id: str, client_id: str) -> object | None: ...
    def read_policy_record(self, business_id: str) -> object | None: ...
    def write_link(self, link: IdentityLink, previous: IdentityLink | None) -> None: ...


class IdentityLinks:
    def __init__(self, store: LinkStore) -> None:
        self._store = store

    def read_link(self, subject: str) -> IdentityLink | None:
        return self._store.read_link(subject)

    def resolve(self, subject: str) -> IdentityLink | None:
        if not subject:
            return None
        link = self._store.read_link(subject)
        if link is None or link.state != LinkState.ACTIVE:
            return None
        if self._store.read_target_subject(link.role, link.business_id,
                                           link.client_id) != subject:
            return None
        if link.role == LinkRole.CLIENT:
            if link.client_id is None or self._store.read_profile(
                    link.business_id, link.client_id) is None:
                return None
        elif self._store.read_policy_record(link.business_id) is None:
            return None
        return link

    def change(self, subject: str, role: LinkRole, business_id: str,
               client_id: str | None, state: LinkState, changed_by: str,
               reason: str, *, expected_version: int | None,
               now: datetime | None = None) -> IdentityLink:
        """Create, activate, or revoke with an optimistic version check.

        Replacing a target first requires revoking its old subject. A subject's
        target is immutable; moving it requires a new Cognito identity.
        """
        previous = self._store.read_link(subject)
        if (previous.version if previous else None) != expected_version:
            raise LinkConflict("Link version changed")
        if previous is None and state == LinkState.REVOKED:
            raise LinkConflict("Cannot revoke an absent link")
        link = IdentityLink(subject, role, business_id, client_id, state,
                            (expected_version or 0) + 1,
                            now or datetime.now(UTC), changed_by, reason)
        if previous is not None and (previous.role, previous.business_id,
                                     previous.client_id) != (role, business_id, client_id):
            raise LinkConflict("Subject target cannot change")
        if state != LinkState.REVOKED:
            if role == LinkRole.CLIENT:
                if client_id is None or self._store.read_profile(business_id, client_id) is None:
                    raise LinkConflict("Client profile does not exist")
            elif self._store.read_policy_record(business_id) is None:
                raise LinkConflict("Business does not exist")
        self._store.write_link(link, previous)
        return link


class DynamoIdentityLinkStore:
    """One subject item and one unique target item in the scheduling table."""

    def __init__(self, client: Any, table_name: str, records: Any) -> None:
        self._client = client
        self._table = table_name
        self._records = records

    @staticmethod
    def _subject_key(subject: str) -> dict[str, Any]:
        return {"PK": {"S": f"ACCOUNT#{sha256(subject.encode()).hexdigest()}"},
                "SK": {"S": "LINK"}}

    @staticmethod
    def _target_key(role: LinkRole, business_id: str,
                    client_id: str | None) -> dict[str, Any]:
        suffix = "OWNER" if role == LinkRole.OWNER else f"CLIENT#{client_id}"
        return {"PK": {"S": f"BUSINESS#{business_id}"},
                "SK": {"S": f"ACCOUNT_TARGET#{suffix}"}}

    def _get(self, key: dict[str, Any]) -> dict[str, Any] | None:
        return self._client.get_item(TableName=self._table, Key=key,
                                     ConsistentRead=True).get("Item")

    def read_link(self, subject: str) -> IdentityLink | None:
        item = self._get(self._subject_key(subject))
        if item is None:
            return None
        try:
            if item["subject"]["S"] != subject:
                return None
            return IdentityLink(
                subject, LinkRole(item["role"]["S"]), item["business_id"]["S"],
                item.get("client_id", {}).get("S"), LinkState(item["state"]["S"]),
                int(item["version"]["N"]), datetime.fromisoformat(item["updated_at"]["S"]),
                item["changed_by"]["S"], item["reason"]["S"])
        except (KeyError, TypeError, ValueError):
            return None

    def read_target_subject(self, role: LinkRole, business_id: str,
                            client_id: str | None) -> str | None:
        item = self._get(self._target_key(role, business_id, client_id))
        return item.get("subject", {}).get("S") if item else None

    def read_profile(self, business_id: str, client_id: str) -> object | None:
        return self._records.read_profile(business_id, client_id)

    def read_policy_record(self, business_id: str) -> object | None:
        return self._records.read_policy_record(business_id)

    def write_link(self, link: IdentityLink, previous: IdentityLink | None) -> None:
        subject_key = self._subject_key(link.subject)
        target_key = self._target_key(link.role, link.business_id, link.client_id)
        item = {**subject_key,
                "subject": {"S": link.subject}, "role": {"S": link.role.value},
                "business_id": {"S": link.business_id},
                "state": {"S": link.state.value}, "version": {"N": str(link.version)},
                "updated_at": {"S": link.updated_at.isoformat()},
                "changed_by": {"S": link.changed_by}, "reason": {"S": link.reason}}
        if link.client_id is not None:
            item["client_id"] = {"S": link.client_id}
        expected = ({"ConditionExpression": "attribute_not_exists(PK)"} if previous is None
                    else {"ConditionExpression": "#version = :old",
                          "ExpressionAttributeNames": {"#version": "version"},
                          "ExpressionAttributeValues": {":old": {"N": str(previous.version)}}})
        writes: list[dict[str, Any]] = [{"Put": {"TableName": self._table,
                                                   "Item": item, **expected}}]
        writes.append({"Put": {
            "TableName": self._table,
            "Item": {**subject_key, "SK": {"S": f"EVENT#{link.version:020d}"},
                     "subject": {"S": link.subject}, "role": {"S": link.role.value},
                     "business_id": {"S": link.business_id},
                     "client_id": {"S": link.client_id or ""},
                     "state": {"S": link.state.value},
                     "updated_at": {"S": link.updated_at.isoformat()},
                     "changed_by": {"S": link.changed_by},
                     "reason": {"S": link.reason}},
            "ConditionExpression": "attribute_not_exists(PK)",
        }})
        if link.state == LinkState.REVOKED:
            writes.append({"Delete": {"TableName": self._table, "Key": target_key,
                                        "ConditionExpression": "subject = :subject",
                                        "ExpressionAttributeValues": {
                                            ":subject": {"S": link.subject}}}})
        elif previous is None or previous.state == LinkState.REVOKED:
            writes.append({"Put": {"TableName": self._table,
                                     "Item": {**target_key, "subject": {"S": link.subject},
                                              "client_id": {"S": link.client_id or ""}},
                                     "ConditionExpression": "attribute_not_exists(PK)"}})
        else:
            writes.append({"ConditionCheck": {
                "TableName": self._table, "Key": target_key,
                "ConditionExpression": "subject = :subject",
                "ExpressionAttributeValues": {":subject": {"S": link.subject}}}})
        if link.role == LinkRole.CLIENT and link.state != LinkState.REVOKED:
            assert link.client_id is not None
            writes.extend([
                {"ConditionCheck": {"TableName": self._table,
                                    "Key": {"PK": {"S": f"BUSINESS#{link.business_id}"},
                                            "SK": {"S": f"CLIENT#{link.client_id}"}},
                                    "ConditionExpression": "attribute_exists(PK)"}},
                {"ConditionCheck": {"TableName": self._table,
                                    "Key": {"PK": {"S": f"BUSINESS#{link.business_id}"},
                                            "SK": {"S": "ERASURE#" + sha256(
                                                link.client_id.encode()).hexdigest()}},
                                    "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as exc:
            if "ConditionalCheckFailed" in str(exc) or "TransactionCanceled" in str(exc):
                raise LinkConflict("Link changed or target is unavailable") from exc
            raise
