"""DynamoDB invitation records and Cognito's email invitation transport."""

from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from typing import Any

from scheduling.account_invitations import ClientInvitation, InvitationDenied, normalize_email
from scheduling.identity_links import IdentityLink


class DynamoClientInvitations:
    def __init__(self, client: Any, table_name: str, records: Any) -> None:
        self._client = client
        self._table = table_name
        self._records = records

    @staticmethod
    def _key(business_id: str, client_id: str) -> dict[str, Any]:
        return {"PK": {"S": f"BUSINESS#{business_id}"},
                "SK": {"S": f"ACCOUNT_INVITE#CLIENT#{client_id}"}}

    def read_profile(self, business_id: str, client_id: str) -> object | None:
        return self._records.read_profile(business_id, client_id)

    def read_invitation(self, business_id: str, client_id: str) -> ClientInvitation | None:
        item = self._client.get_item(TableName=self._table,
                                     Key=self._key(business_id, client_id),
                                     ConsistentRead=True).get("Item")
        if item is None:
            return None
        try:
            return ClientInvitation(
                business_id, client_id, item["subject"]["S"], item["email_hash"]["S"],
                datetime.fromisoformat(item["expires_at"]["S"]),
                datetime.fromisoformat(item["issued_at"]["S"]),
                item["issued_by"]["S"],
                datetime.fromisoformat(item["consumed_at"]["S"])
                if "consumed_at" in item else None,
                datetime.fromisoformat(item["sent_at"]["S"])
                if "sent_at" in item else None,
                datetime.fromisoformat(item["revoked_at"]["S"])
                if "revoked_at" in item else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvitationDenied("Invitation record is invalid") from exc

    def write_invitation(self, invitation: ClientInvitation,
                         previous: ClientInvitation | None) -> None:
        item = {**self._key(invitation.business_id, invitation.client_id),
                "client_id": {"S": invitation.client_id},
                "subject": {"S": invitation.subject},
                "email_hash": {"S": invitation.email_hash},
                "expires_at": {"S": invitation.expires_at.isoformat()},
                "issued_at": {"S": invitation.issued_at.isoformat()},
                "issued_by": {"S": invitation.issued_by}}
        condition = ("attribute_not_exists(PK)" if previous is None
                     else "expires_at = :old AND attribute_exists(revoked_at)")
        values = ({} if previous is None else {
            "ExpressionAttributeValues": {":old": {"S": previous.expires_at.isoformat()}}})
        business_id = invitation.business_id
        client_id = invitation.client_id
        try:
            self._client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self._table, "Item": item,
                         "ConditionExpression": condition, **values}},
                {"ConditionCheck": {
                    "TableName": self._table,
                    "Key": {"PK": {"S": f"BUSINESS#{business_id}"},
                            "SK": {"S": f"CLIENT#{client_id}"}},
                    "ConditionExpression": "attribute_exists(PK)",
                }},
                {"ConditionCheck": {
                    "TableName": self._table,
                    "Key": {"PK": {"S": f"BUSINESS#{business_id}"},
                            "SK": {"S": "ERASURE#" + sha256(client_id.encode()).hexdigest()}},
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
            ])
        except Exception as exc:
            if "ConditionalCheckFailed" in str(exc) or "TransactionCanceled" in str(exc):
                raise InvitationDenied("Invitation changed") from exc
            raise

    def mark_sent(self, invitation: ClientInvitation, now: datetime,
                  expires_at: datetime) -> ClientInvitation:
        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(invitation.business_id, invitation.client_id),
                UpdateExpression="SET sent_at = :now, expires_at = :new_expiry",
                ConditionExpression="subject = :sub AND expires_at = :expiry "
                                    "AND attribute_not_exists(sent_at) "
                                    "AND attribute_not_exists(consumed_at) "
                                    "AND attribute_not_exists(revoked_at)",
                ExpressionAttributeValues={
                    ":now": {"S": now.isoformat()},
                    ":new_expiry": {"S": expires_at.isoformat()},
                    ":sub": {"S": invitation.subject},
                    ":expiry": {"S": invitation.expires_at.isoformat()},
                },
            )
        except Exception as exc:
            if "ConditionalCheckFailed" in str(exc):
                raise InvitationDenied("Invitation delivery changed") from exc
            raise
        return replace(invitation, sent_at=now, expires_at=expires_at)

    def activate_link(self, invitation: ClientInvitation,
                      pending: IdentityLink, now: datetime) -> None:
        business_id = invitation.business_id
        client_id = invitation.client_id
        subject = invitation.subject
        subject_key = {"PK": {"S": f"ACCOUNT#{sha256(subject.encode()).hexdigest()}"},
                       "SK": {"S": "LINK"}}
        target_key = {"PK": {"S": f"BUSINESS#{business_id}"},
                      "SK": {"S": f"ACCOUNT_TARGET#CLIENT#{client_id}"}}
        event_key = {**subject_key, "SK": {"S": f"EVENT#{pending.version + 1:020d}"}}
        fence_key = {"PK": {"S": f"BUSINESS#{business_id}"},
                     "SK": {"S": "ERASURE#" + sha256(client_id.encode()).hexdigest()}}
        try:
            self._client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": self._table, "Key": self._key(business_id, client_id),
                    "UpdateExpression": "SET consumed_at = :now",
                    "ConditionExpression": "subject = :sub AND expires_at > :now "
                                           "AND attribute_exists(sent_at) "
                                           "AND attribute_not_exists(revoked_at) "
                                           "AND attribute_not_exists(consumed_at)",
                    "ExpressionAttributeValues": {":now": {"S": now.isoformat()},
                                                  ":sub": {"S": subject}},
                }},
                {"Update": {
                    "TableName": self._table, "Key": subject_key,
                    "UpdateExpression": "SET #state = :active, #version = :next, "
                                        "updated_at = :now, changed_by = :sub, reason = :reason",
                    "ConditionExpression": "#state = :pending AND #version = :old "
                                           "AND subject = :sub AND role = :role "
                                           "AND business_id = :business AND client_id = :client",
                    "ExpressionAttributeNames": {"#state": "state", "#version": "version"},
                    "ExpressionAttributeValues": {
                        ":active": {"S": "active"}, ":pending": {"S": "pending"},
                        ":old": {"N": str(pending.version)},
                        ":next": {"N": str(pending.version + 1)},
                        ":now": {"S": now.isoformat()}, ":sub": {"S": subject},
                        ":reason": {"S": "invited email verified"},
                        ":role": {"S": "client"}, ":business": {"S": business_id},
                        ":client": {"S": client_id},
                    },
                }},
                {"Put": {
                    "TableName": self._table,
                    "Item": {**event_key, "subject": {"S": subject},
                             "role": {"S": "client"}, "business_id": {"S": business_id},
                             "client_id": {"S": client_id}, "state": {"S": "active"},
                             "updated_at": {"S": now.isoformat()},
                             "changed_by": {"S": subject},
                             "reason": {"S": "invited email verified"}},
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
                {"ConditionCheck": {
                    "TableName": self._table, "Key": target_key,
                    "ConditionExpression": "subject = :sub",
                    "ExpressionAttributeValues": {":sub": {"S": subject}},
                }},
                {"ConditionCheck": {
                    "TableName": self._table,
                    "Key": {"PK": {"S": f"BUSINESS#{business_id}"},
                            "SK": {"S": f"CLIENT#{client_id}"}},
                    "ConditionExpression": "attribute_exists(PK)",
                }},
                {"ConditionCheck": {
                    "TableName": self._table, "Key": fence_key,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
            ])
        except Exception as exc:
            if "ConditionalCheckFailed" in str(exc) or "TransactionCanceled" in str(exc):
                raise InvitationDenied("Invitation changed, expired, or was already used") from exc
            raise

    def revoke_invitation(self, invitation: ClientInvitation, now: datetime) -> None:
        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(invitation.business_id, invitation.client_id),
                UpdateExpression="SET revoked_at = :now",
                ConditionExpression="subject = :sub AND expires_at = :expiry "
                                    "AND attribute_not_exists(revoked_at)",
                ExpressionAttributeValues={
                    ":now": {"S": now.isoformat()},
                    ":sub": {"S": invitation.subject},
                    ":expiry": {"S": invitation.expires_at.isoformat()},
                },
            )
        except Exception as exc:
            if "ConditionalCheckFailed" in str(exc):
                raise InvitationDenied("Invitation changed") from exc
            raise


class CognitoAccountDirectory:
    def __init__(self, client: Any, user_pool_id: str) -> None:
        self._client = client
        self._pool = user_pool_id

    @staticmethod
    def _subject(user: dict[str, Any]) -> str:
        attributes = user.get("UserAttributes", user.get("Attributes", ()))
        subject = next((entry["Value"] for entry in attributes
                        if entry.get("Name") == "sub"), None)
        if not isinstance(subject, str) or not subject:
            raise InvitationDenied("Cognito account has no subject")
        return subject

    def create_or_find_invitee(self, email: str) -> tuple[str, bool]:
        try:
            user = self._client.admin_get_user(UserPoolId=self._pool, Username=email)
        except Exception as exc:
            if "UserNotFoundException" not in str(exc):
                raise
        else:
            attributes = {entry.get("Name"): entry.get("Value")
                          for entry in user.get("UserAttributes", ())}
            if (user.get("Enabled") is not True
                    or not isinstance(attributes.get("email"), str)
                    or normalize_email(attributes["email"]) != normalize_email(email)
                    or attributes.get("email_verified") != "true"):
                raise InvitationDenied("Existing account email is not verified")
            return self._subject(user), False
        user = self._client.admin_create_user(
            UserPoolId=self._pool, Username=email,
            # The temporary password is sent only to this address on RESEND. A new
            # account cannot sign in and activate without receiving that email.
            UserAttributes=[{"Name": "email", "Value": email},
                            {"Name": "email_verified", "Value": "true"}],
            MessageAction="SUPPRESS",
        )["User"]
        return self._subject(user), True

    def send_invitation(self, email: str, *, created: bool) -> None:
        del created
        self._client.admin_create_user(
            UserPoolId=self._pool, Username=email, MessageAction="RESEND",
            DesiredDeliveryMediums=["EMAIL"],
        )
