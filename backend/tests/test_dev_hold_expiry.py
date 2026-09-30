"""Opt-in proof that the hold-expiry worker expires synthetic due holds and ignores stale entries.

Dev mode invokes the deployed ``scheduling-dev`` Lambda. With only ``DYNAMODB_LOCAL_URL`` set,
the same assertions run the real handler in-process against DynamoDB Local. Both modes reuse
the guards and run-scoped cleanup of the #80 harness in ``test_dynamodb_local_races``.
"""

import json
import os
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from test_dynamodb_local_races import (
    DEV_REGION,
    RaceEnv,
    _business_items,
    _dev_env,
    _get,
    _local_env,
    _wait_until,
)

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository, encode_policy
from scheduling.domain.availability import pilot_policy
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.workers.expiry import expire_due_handler

FUNCTION_PREFIX = "scheduling-dev-"
FUNCTION_PATTERN = re.compile(r"^scheduling-dev-[A-Za-z0-9_-]{1,64}$")
LAMBDA_ENDPOINT = "https://lambda.us-west-1.amazonaws.com"
MAX_INVOCATIONS = 5
# Far in the past, like #80, so this run's due-index keys sort ahead of other records.
CREATED = datetime(2001, 9, 10, 12, tzinfo=UTC)
VISIT_DAY_HOURS_UTC = (16, 18, 20, 22)  # 09:00, 11:00, 13:00, 15:00 Pacific on Wed 2001-09-12
Invoke = Callable[[], dict[str, Any]]


def _dev_function_name() -> str:
    name = os.environ.get("SCHEDULING_DEV_HOLD_EXPIRY_FUNCTION", "")
    if not name.startswith(FUNCTION_PREFIX) or not FUNCTION_PATTERN.fullmatch(name):
        raise ValueError(
            f"SCHEDULING_DEV_HOLD_EXPIRY_FUNCTION must be a function name starting with "
            f"{FUNCTION_PREFIX!r} (not an ARN or another function)"
        )
    return name


def _lambda_invoker(name: str) -> Invoke:
    client = boto3.client("lambda", region_name=DEV_REGION)
    if client.meta.endpoint_url != LAMBDA_ENDPOINT:
        raise ValueError(f"Unexpected Lambda endpoint {client.meta.endpoint_url!r}")

    def invoke() -> dict[str, Any]:
        response = client.invoke(
            FunctionName=name, InvocationType="RequestResponse", Payload=b"{}",
        )
        body = response["Payload"].read()
        assert response["StatusCode"] == 200
        assert "FunctionError" not in response, body[:500]
        result = json.loads(body)
        assert isinstance(result, dict)
        print(f"hold_expiry report: {json.dumps(result, sort_keys=True)}", flush=True)
        return result

    return invoke


def _adopt_hold_ids(env: RaceEnv) -> None:
    """Hold ids are generated; register them from the run's own items so cleanup finds them."""
    try:
        for item in _business_items(env):
            for attribute in ("hold_id", "event_id", "appointment_id"):
                if attribute in item and item[attribute]["S"] not in env.appointment_ids:
                    env.appointment_ids.append(item[attribute]["S"])
    except (BotoCoreError, ClientError) as exc:  # the business partition is still scrubbed
        print(f"Could not adopt hold ids for cleanup: {exc}", flush=True)


@pytest.fixture
def expiry_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[RaceEnv, Invoke]]:
    if os.environ.get("SCHEDULING_DEV_TABLE"):
        for env in _dev_env():  # table, region, endpoint guards, CI skip, run id, cleanup
            try:
                invoke = _lambda_invoker(_dev_function_name())
                yield env, invoke
            finally:
                _adopt_hold_ids(env)
        return
    for env in _local_env():
        url = os.environ["DYNAMODB_LOCAL_URL"]
        monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", url)
        monkeypatch.setenv("AWS_DEFAULT_REGION", DEV_REGION)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic")
        monkeypatch.setenv("SCHEDULING_TABLE_NAME", env.table)
        yield env, lambda: expire_due_handler({}, None)


def _create_hold(env: RaceEnv, service: HoldService, index: int) -> str:
    command = CreateHold(
        env.business, "synthetic-client", "synthetic-client", f"{env.run}-hold-{index}",
        datetime(2001, 9, 12, VISIT_DAY_HOURS_UTC[index], tzinfo=UTC), 60,
    )
    hold = service.create(command, CREATED + timedelta(minutes=index))
    env.appointment_ids.append(hold.hold_id)
    assert hold.hold_expires_at < datetime(2002, 1, 1, tzinfo=UTC)
    return hold.hold_id


def _hold_items(env: RaceEnv, hold_id: str) -> list[dict[str, Any]]:
    items = [item for item in _business_items(env) if hold_id in json.dumps(item, sort_keys=True)]
    metadata = _get(env, f"APPOINTMENT#{hold_id}", "META")
    assert metadata is not None
    return sorted([*items, metadata], key=lambda item: (item["PK"]["S"], item["SK"]["S"]))


def test_hold_expiry_worker_expires_due_holds_and_ignores_stale_entry(
    expiry_env: tuple[RaceEnv, Invoke],
) -> None:
    env, invoke = expiry_env
    repo = DynamoDBCalendarRepository(env.client, env.table)
    env.client.put_item(TableName=env.table, Item={
        **repo._business_key(env.business, "POLICY#SCHEDULING"),
        "payload": {"S": encode_policy(pilot_policy())}, "version": {"N": "1"},
    })
    service = HoldService(repo)
    due_ids = [_create_hold(env, service, index) for index in range(3)]
    stale_id = _create_hold(env, service, 3)
    # A stale entry: the hold was approved after creation, yet its due-index keys remain.
    # The worker rereads the appointment and skips anything that is not a pending,
    # past-due hold.
    env.client.update_item(
        TableName=env.table,
        Key={"PK": {"S": f"APPOINTMENT#{stale_id}"}, "SK": {"S": "META"}},
        UpdateExpression="SET #status = :confirmed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":confirmed": {"S": "CONFIRMED"}},
    )
    stale_before = _hold_items(env, stale_id)
    now = datetime.now(UTC)
    _wait_until(
        lambda: {*due_ids, stale_id} <= set(repo.due_hold_ids(now, 500)),
        "Synthetic holds did not appear in HoldDueIndex",
    )

    reports: list[dict[str, Any]] = []
    for _ in range(MAX_INVOCATIONS):
        reports.append(invoke())
        expired = [_get(env, f"APPOINTMENT#{hold_id}", "META") for hold_id in due_ids]
        if all(m is not None and m["status"]["S"] == "EXPIRED" for m in expired) and any(
            report["stale"] for report in reports
        ):
            break
    for report in reports:
        assert set(report) == {"examined", "expired", "stale", "oldest_overdue_seconds"}
        assert report["examined"] == report["expired"] + report["stale"]
        assert report["expired"] >= 0 and report["stale"] >= 0
        assert (report["oldest_overdue_seconds"] > 0) == (report["expired"] > 0)
    assert sum(report["expired"] for report in reports) >= len(due_ids)
    assert sum(report["stale"] for report in reports) >= 1

    items = _business_items(env)
    for hold_id in due_ids:
        metadata = _get(env, f"APPOINTMENT#{hold_id}", "META")
        assert metadata is not None
        assert metadata["status"]["S"] == "EXPIRED" and metadata["version"]["N"] == "2"
        assert "hold_due_pk" not in metadata
        audits = [item for item in items if item["SK"]["S"].startswith("AUDIT#")
                  and item.get("appointment_id", {}).get("S") == hold_id]
        assert [(a["action"]["S"], a["actor_id"]["S"]) for a in audits] == [
            ("expire", "system:hold-expiry")
        ]
        notices = [item for item in items
                   if item.get("appointment_id", {}).get("S") == hold_id
                   and item["SK"]["S"].startswith("OUTBOX#")]
        assert len(notices) == 1
        assert notices[0]["template"]["S"] == "expire"
        assert notices[0]["recipient"]["S"] == "client"
        assert notices[0]["delivery_state"]["S"] == "PENDING"
        assert "outbox_due_pk" in notices[0]

    assert _hold_items(env, stale_id) == stale_before
    stale_metadata = _get(env, f"APPOINTMENT#{stale_id}", "META")
    assert stale_metadata is not None and stale_metadata["status"]["S"] == "CONFIRMED"
    assert not [item for item in items if item.get("appointment_id", {}).get("S") == stale_id]

    # Nothing dispatched: every outbox item of this run is still an unattempted PENDING intent.
    outbox = [item for item in items if item["SK"]["S"].startswith("OUTBOX#")]
    assert len(outbox) == 2 * 4 + len(due_ids)  # two creation notices per hold, one expiry each
    for item in outbox:
        assert item["delivery_state"]["S"] == "PENDING"
        assert item["attempts"]["N"] == "0"
        assert "provider_id" not in item
