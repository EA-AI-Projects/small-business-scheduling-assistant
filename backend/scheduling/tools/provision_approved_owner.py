"""Bind an explicitly approved, email-verified Cognito owner to one business."""

import argparse
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.identity_links import (
    DynamoIdentityLinkStore,
    IdentityLinks,
    LinkRole,
    LinkState,
)


def approved_owner_subject(user: dict[str, Any], email: str) -> str:
    attributes = {item["Name"]: item["Value"] for item in user["UserAttributes"]}
    if (user.get("Enabled") is not True or user.get("UserStatus") != "CONFIRMED"
            or attributes.get("email_verified") != "true"
            or attributes.get("email", "").casefold() != email.casefold()
            or not attributes.get("sub")):
        raise ValueError("Owner Cognito identity and verified email are required")
    return str(attributes["sub"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--business", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--approval-reference", required=True,
                        help="Reference to the operator's recorded owner approval")
    args = parser.parse_args()
    cognito = boto3.client("cognito-idp")
    user = cognito.admin_get_user(UserPoolId=args.pool, Username=args.email)
    subject = approved_owner_subject(user, args.email)
    dynamo = boto3.client("dynamodb")
    records = DynamoDBCalendarRepository(dynamo, args.table)
    links = IdentityLinks(DynamoIdentityLinkStore(dynamo, args.table, records))
    links.change(subject, LinkRole.OWNER, args.business, None,
                 LinkState.ACTIVE, args.actor,
                 f"owner approval {args.approval_reference}", expected_version=None)
    print("Approved owner link created")


if __name__ == "__main__":
    main()
