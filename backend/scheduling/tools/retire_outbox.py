"""Reviewed one-off: retire unsent synthetic outbox records before live SMS (issue #91).

Moves PENDING and RETRYABLE records created before a cutoff to the terminal FAILED state with
error code RETIRED_BEFORE_LIVE_SMS, so the dispatcher never sends them. Dry-run is the default.
It only runs against the deployed ``scheduling-dev`` table in us-west-1 at the real regional
endpoint, and it never prints phone numbers or message bodies (it never reads them).

    python -m scheduling.tools.retire_outbox --table scheduling-dev \\
        --business-id dev-synthetic --cutoff 2026-10-02T00:00:00+00:00 [--execute]
"""

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]

from scheduling.adapters.outbox_aws import (
    RETIRED_BEFORE_LIVE_SMS,
    DynamoOutboxStore,
    RetireOutcome,
    RetireResult,
)

DEV_TABLE = "scheduling-dev"
DEV_REGION = "us-west-1"
DEV_ENDPOINT = "https://dynamodb.us-west-1.amazonaws.com"
# The dedicated dev account; duplicated from EXPECTED_ACCOUNT in scripts/dev/lib.sh.
DEV_ACCOUNT = "214965372605"


def dev_client() -> Any:
    """Standard credential chain (for example AWS_PROFILE=scheduling-dev-admin)."""
    try:
        account = boto3.client("sts", region_name=DEV_REGION).get_caller_identity()["Account"]
    except Exception as error:  # no credentials, expired SSO session, network
        raise ValueError(f"could not verify the AWS account ({type(error).__name__})") from error
    if account != DEV_ACCOUNT:
        raise ValueError(f"caller account is not {DEV_ACCOUNT}")
    client = boto3.client("dynamodb", region_name=DEV_REGION)
    # Also catches AWS_ENDPOINT_URL_DYNAMODB and a profile-level endpoint_url.
    if client.meta.endpoint_url != DEV_ENDPOINT:
        raise ValueError(f"Unexpected DynamoDB endpoint {client.meta.endpoint_url!r}")
    return client


def _cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("cutoff must include a UTC offset, e.g. +00:00")
    return parsed.astimezone(UTC)


def _stamp(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else "-"


def _line(outcome: RetireOutcome) -> str:
    return (
        f"  ...{outcome.outbox_id[-8:]}  template={outcome.template}  "
        f"recipient={outcome.recipient}  state={outcome.state.value}  "
        f"created={_stamp(outcome.created_at)}  due={_stamp(outcome.due_at)}  "
        f"-> {outcome.result.value}"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = dev_client,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--table", required=True)
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--cutoff", required=True, type=_cutoff,
                        help="ISO timestamp with offset; records created before it are retired")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="list only (the default)")
    mode.add_argument("--execute", action="store_true", help="write the retirements")
    args = parser.parse_args(argv)

    if args.table != DEV_TABLE:
        out(f"Refusing: --table must be {DEV_TABLE!r}, never another table")
        return 2
    try:
        client = client_factory()
    except ValueError as error:
        out(f"Refusing: {error}")
        return 2
    if getattr(client.meta, "region_name", DEV_REGION) != DEV_REGION:
        out(f"Refusing: region must be {DEV_REGION!r}")
        return 2

    store = DynamoOutboxStore(client, args.table)
    now = clock()
    execute: bool = args.execute
    out(f"{'EXECUTE' if execute else 'DRY RUN'}: table={args.table} "
        f"business={args.business_id} cutoff={_stamp(args.cutoff)}")
    outcomes = store.retire_pending_before(
        args.business_id, args.cutoff, RETIRED_BEFORE_LIVE_SMS, now, execute=execute,
    )
    for outcome in outcomes:
        out(_line(outcome))
    counts = Counter(o.result for o in outcomes)
    out("Summary: " + ", ".join(
        f"{result.value}={counts.get(result, 0)}" for result in RetireResult
    ) + f" (total {len(outcomes)})")
    if not execute:
        out("Nothing was written. Re-run with --execute to retire the records above.")
        return 0

    remaining = store.pending_before(args.business_id, args.cutoff)
    out(f"Confirmation: {len(remaining)} unsent record(s) before the cutoff remain in "
        "the due index")
    for record in remaining:
        out(f"  ...{record.outbox_id[-8:]}  state={record.state.value}")
    return 1 if remaining else 0


if __name__ == "__main__":
    sys.exit(main())
