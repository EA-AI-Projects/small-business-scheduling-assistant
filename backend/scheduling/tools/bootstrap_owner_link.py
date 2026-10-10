"""One-time, explicit pilot owner link creation; never runs during deployment."""

import argparse

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.identity_links import (
    DynamoIdentityLinkStore,
    IdentityLinks,
    LinkRole,
    LinkState,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--business", required=True)
    parser.add_argument("--actor", required=True,
                        help="Operator identifier for the link audit record")
    args = parser.parse_args()
    client = boto3.client("dynamodb")
    records = DynamoDBCalendarRepository(client, args.table)
    links = IdentityLinks(DynamoIdentityLinkStore(client, args.table, records))
    links.change(args.subject, LinkRole.OWNER, args.business, None,
                 LinkState.ACTIVE, args.actor, "pilot owner migration",
                 expected_version=None)
    print("Pilot owner link created")


if __name__ == "__main__":
    main()
