"""Retiring stale outbox records: transition, conditions, cutoff, dry-run and the dev guards."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scheduling.adapters.outbox_aws import (
    RETIRED_BEFORE_LIVE_SMS,
    DynamoOutboxStore,
    RetireResult,
    due_keys,
)
from scheduling.domain.outbox import DeliveryState
from scheduling.tools import retire_outbox

NOW = datetime(2026, 10, 2, 16, tzinfo=UTC)
CUTOFF = datetime(2026, 10, 2, 0, tzinfo=UTC)
OLD = CUTOFF - timedelta(hours=5)
BUSINESS = "synthetic-business"


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _item(outbox_id: str, state: DeliveryState, created: datetime, *,
          business: str = BUSINESS, lease: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "PK": {"S": f"BUSINESS#{business}"}, "SK": {"S": f"OUTBOX#{outbox_id}"},
        "outbox_id": {"S": outbox_id}, "entity_id": {"S": "synthetic-entity"},
        "recipient": {"S": "owner"}, "template": {"S": "block_time"},
        "event_version": {"N": "1"}, "delivery_state": {"S": state.value},
        "created_at": {"S": _iso(created)},
        "next_attempt_at": {"S": _iso(created)}, "dispatch_after": {"S": _iso(created)},
        **due_keys(state, created, outbox_id),
    }
    if lease:
        item["lease_token"] = {"S": lease}
    return item


class FakeTable:
    """In-memory table: due-index query, strong get, and the retire update conditions."""

    class meta:
        region_name = "us-west-1"

    def __init__(self, *items: dict[str, Any]) -> None:
        self.items = {i["SK"]["S"]: i for i in items}
        self.updates = 0
        self.before_update: Any = None

    def query(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["IndexName"] == "OutboxDueIndex"
        pk = kwargs["ExpressionAttributeValues"][":state"]["S"]
        rows = [i for i in self.items.values()
                if i.get("outbox_due_pk", {}).get("S") == pk]
        rows.sort(key=lambda i: i["outbox_due_sk"]["S"])
        start = kwargs.get("ExclusiveStartKey")
        if start:
            rows = rows[[r["SK"] for r in rows].index(start["SK"]) + 1:]
        page = rows[:1]  # one per page to exercise pagination
        response: dict[str, Any] = {
            "Items": [{"PK": r["PK"], "SK": r["SK"]} for r in page]}
        if len(rows) > 1:
            response["LastEvaluatedKey"] = {"PK": page[0]["PK"], "SK": page[0]["SK"]}
        return response

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self.items.get(kwargs["Key"]["SK"]["S"])
        return {"Item": item} if item else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.updates += 1
        if self.before_update:
            self.before_update(self)
        item = self.items[kwargs["Key"]["SK"]["S"]]
        values = kwargs["ExpressionAttributeValues"]
        ok = (
            item["delivery_state"] == values[":state"]
            and item.get("next_attempt_at") == values[":old_attempt"]
            and item.get("dispatch_after") == values[":old_dispatch"]
            and "lease_token" not in item
        )
        if not ok:
            error = Exception("conflict")
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}  # type: ignore[attr-defined]
            raise error
        for name in ("outbox_due_pk", "outbox_due_sk", "dispatch_after",
                     "next_attempt_at", "lease_token"):
            item.pop(name, None)
        item.update({
            "delivery_state": values[":failed"], "last_error_code": values[":error"],
            "last_failed_at": values[":now"], "retired_at": values[":now"],
        })
        return {}


def _store(table: FakeTable) -> DynamoOutboxStore:
    return DynamoOutboxStore(table, "scheduling-dev")


def _run(store: DynamoOutboxStore, *, execute: bool) -> Any:
    return store.retire_pending_before(
        BUSINESS, CUTOFF, RETIRED_BEFORE_LIVE_SMS, NOW, execute=execute)


def test_retire_moves_pending_and_retryable_to_terminal_failed() -> None:
    table = FakeTable(
        _item("o-1", DeliveryState.PENDING, OLD),
        _item("o-2", DeliveryState.RETRYABLE, OLD + timedelta(hours=1)),
    )
    outcomes = _run(_store(table), execute=True)
    assert [o.result for o in outcomes] == [RetireResult.RETIRED] * 2
    for key in ("OUTBOX#o-1", "OUTBOX#o-2"):
        item = table.items[key]
        assert item["delivery_state"] == {"S": "FAILED"}
        assert item["last_error_code"] == {"S": "RETIRED_BEFORE_LIVE_SMS"}
        assert item["retired_at"] == {"S": _iso(NOW)}
        assert item["last_failed_at"] == {"S": _iso(NOW)}
        for gone in ("outbox_due_pk", "outbox_due_sk", "dispatch_after",
                     "next_attempt_at", "lease_token"):
            assert gone not in item
    # Out of the due index, so the dispatcher and consumer never see them again.
    assert list(_store(table).due(NOW, 10)) == []
    assert _store(table).pending_before(BUSINESS, CUTOFF) == []


def test_dry_run_writes_nothing() -> None:
    table = FakeTable(_item("o-1", DeliveryState.PENDING, OLD))
    outcomes = _run(_store(table), execute=False)
    assert [o.result for o in outcomes] == [RetireResult.WOULD_RETIRE]
    assert table.updates == 0
    assert table.items["OUTBOX#o-1"]["delivery_state"] == {"S": "PENDING"}


def test_cutoff_and_business_are_respected() -> None:
    table = FakeTable(
        _item("old", DeliveryState.PENDING, OLD),
        _item("new", DeliveryState.PENDING, CUTOFF + timedelta(minutes=1)),
        _item("at", DeliveryState.PENDING, CUTOFF),
        _item("other", DeliveryState.PENDING, OLD, business="another-business"),
    )
    outcomes = _run(_store(table), execute=True)
    assert [o.outbox_id for o in outcomes] == ["old"]
    for key in ("new", "at", "other"):
        assert table.items[f"OUTBOX#{key}"]["delivery_state"] == {"S": "PENDING"}


def test_leased_sending_record_is_reported_and_untouched() -> None:
    table = FakeTable(_item("o-1", DeliveryState.SENDING, OLD, lease="token"))
    outcomes = _run(_store(table), execute=True)
    assert [o.result for o in outcomes] == [RetireResult.SKIPPED_LEASED]
    assert table.updates == 0
    assert table.items["OUTBOX#o-1"]["delivery_state"] == {"S": "SENDING"}


def test_concurrent_change_is_a_reported_conflict_not_a_crash() -> None:
    table = FakeTable(
        _item("o-1", DeliveryState.PENDING, OLD),
        _item("o-2", DeliveryState.PENDING, OLD + timedelta(hours=1)),
    )

    def claim_first(t: FakeTable) -> None:
        if t.updates == 1:  # another worker leases the record just before our write
            t.items["OUTBOX#o-1"]["delivery_state"] = {"S": "SENDING"}
            t.items["OUTBOX#o-1"]["lease_token"] = {"S": "token"}

    table.before_update = claim_first
    outcomes = _run(_store(table), execute=True)
    assert [o.result for o in outcomes] == [RetireResult.CONFLICT, RetireResult.RETIRED]
    assert table.items["OUTBOX#o-1"]["delivery_state"] == {"S": "SENDING"}
    assert table.items["OUTBOX#o-2"]["delivery_state"] == {"S": "FAILED"}


# --- CLI ---------------------------------------------------------------------------------

ARGS = ["--table", "scheduling-dev", "--business-id", BUSINESS,
        "--cutoff", CUTOFF.isoformat()]


def _cli(table: FakeTable, args: list[str]) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = retire_outbox.main(args, client_factory=lambda: table, clock=lambda: NOW,
                              out=lines.append)
    return code, lines


def test_cli_defaults_to_dry_run_and_prints_no_contact_data() -> None:
    table = FakeTable(_item("o-abcdef12", DeliveryState.PENDING, OLD))
    code, lines = _cli(table, ARGS)
    text = "\n".join(lines)
    assert code == 0 and table.updates == 0
    assert "DRY RUN" in text and "...abcdef12" in text and "template=block_time" in text
    assert "recipient=owner" in text and "WOULD_RETIRE" in text
    assert "synthetic-entity" not in text


def test_cli_execute_summarizes_and_confirms_none_remain() -> None:
    table = FakeTable(_item("o-abcdef12", DeliveryState.PENDING, OLD))
    code, lines = _cli(table, [*ARGS, "--execute"])
    text = "\n".join(lines)
    assert code == 0
    assert "RETIRED=1" in text and "0 unsent record(s)" in text
    assert table.items["OUTBOX#o-abcdef12"]["delivery_state"] == {"S": "FAILED"}


def test_cli_execute_exits_nonzero_when_a_leased_record_remains() -> None:
    table = FakeTable(_item("o-1", DeliveryState.SENDING, OLD, lease="t"))
    code, lines = _cli(table, [*ARGS, "--execute"])
    assert code == 1 and "SKIPPED_LEASED=1" in "\n".join(lines)


def test_cli_refuses_wrong_table_region_and_endpoint() -> None:
    table = FakeTable(_item("o-1", DeliveryState.PENDING, OLD))
    code, _ = _cli(table, ["--table", "scheduling-prod", *ARGS[2:], "--execute"])
    assert code == 2

    class WrongRegion(FakeTable):
        class meta:
            region_name = "us-east-1"

    wrong = WrongRegion(_item("o-1", DeliveryState.PENDING, OLD))
    code, _ = _cli(wrong, [*ARGS, "--execute"])
    assert code == 2 and wrong.updates == 0

    def bad_endpoint() -> Any:
        raise ValueError("Unexpected DynamoDB endpoint 'http://localhost:8000'")

    lines: list[str] = []
    assert retire_outbox.main(ARGS, client_factory=bad_endpoint, out=lines.append) == 2
    assert table.updates == 0


def test_dev_client_refuses_the_wrong_aws_account(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class Sts:
        def get_caller_identity(self) -> dict[str, str]:
            return {"Account": "000000000000"}

    def fake_client(service: str, **_: Any) -> Any:
        created.append(service)
        return Sts()

    monkeypatch.setattr(retire_outbox.boto3, "client", fake_client)
    lines: list[str] = []
    assert retire_outbox.main(ARGS, out=lines.append) == 2
    assert created == ["sts"]  # never got as far as building a DynamoDB client
    assert "caller account" in "\n".join(lines)


def test_dev_client_refuses_when_the_account_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_client(service: str, **_: Any) -> Any:
        raise RuntimeError("no credentials")

    monkeypatch.setattr(retire_outbox.boto3, "client", fake_client)
    assert retire_outbox.main(ARGS, out=lambda _: None) == 2
