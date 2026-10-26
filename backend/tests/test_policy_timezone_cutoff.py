"""Policy changes retain the no-catch-up cutoff for visit reminders."""

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from scheduling.adapters.dynamodb import encode_policy
from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.availability import pilot_policy
from scheduling.owner_api import OwnerPrincipal, create_owner_app


def test_owner_timezone_edits_refresh_cutoff_but_other_edits_and_replays_preserve_it():
    now = datetime(2026, 10, 26, 16, tzinfo=UTC)
    repository = InMemoryCalendarRepository()
    api = TestClient(create_owner_app(
        repository, lambda token: OwnerPrincipal("owner", "pilot"), lambda: now))
    base = "/v1/owner/businesses/pilot/policy"
    headers = {"Authorization": "Bearer synthetic", "Idempotency-Key": "seed"}
    assert api.post(f"{base}/seed", headers=headers).status_code == 200
    assert repository.read_policy_record("pilot").timezone_updated_at == now
    original = now
    now = datetime(2026, 10, 26, 17, tzinfo=UTC)
    policy = json.loads(encode_policy(pilot_policy()))
    policy["timezone"] = "America/New_York"
    change = {"expected_revision": 1, "expected_version": 1, "policy": policy}
    headers["Idempotency-Key"] = "timezone"
    response = api.put(base, json=change, headers=headers)
    assert response.status_code == 200, response.text
    cutoff = now
    assert repository.read_policy_record("pilot").timezone_updated_at == cutoff
    assert cutoff != original
    now = datetime(2026, 10, 26, 18, tzinfo=UTC)
    assert api.put(base, json=change, headers=headers).json() == response.json()
    policy["booking_horizon_days"] += 1
    headers["Idempotency-Key"] = "horizon"
    change = {"expected_revision": 2, "expected_version": 2, "policy": policy}
    assert api.put(base, json=change, headers=headers).status_code == 200
    assert repository.read_policy_record("pilot").timezone_updated_at == cutoff
    headers["Idempotency-Key"] = "noop"
    change.update(expected_revision=3, expected_version=3)
    assert api.put(base, json=change, headers=headers).status_code == 200
    assert repository.read_policy_record("pilot").timezone_updated_at == cutoff


def test_dynamo_policy_and_replay_roundtrip_cutoff_and_accept_legacy_records():
    from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
    from scheduling.domain.owner_policy import (
        PolicyCommand, PolicyCommit, PolicyRecord, PolicyResult,
    )

    class SyntheticDynamo:
        """Storage boundary fake; transaction concurrency is covered elsewhere."""
        def __init__(self):
            self.items = {}

        @staticmethod
        def key(item):
            return item["PK"]["S"], item["SK"]["S"]

        def transact_write_items(self, *, TransactItems):
            for write in TransactItems:
                if "Put" in write:
                    item = write["Put"]["Item"]
                    self.items[self.key(item)] = item

        def get_item(self, *, TableName, Key, ConsistentRead):
            return {"Item": self.items[self.key(Key)]} if self.key(Key) in self.items else {}

    database = SyntheticDynamo()
    now = datetime(2026, 10, 26, 17, tzinfo=UTC)
    repository = DynamoDBCalendarRepository(database, "synthetic")
    command = PolicyCommand("pilot", "owner", "seed", 0, None, pilot_policy())
    result = PolicyResult(PolicyRecord(pilot_policy(), 1, now), 1)
    repository.commit_policy(0, PolicyCommit(command, command.request_hash(), result, now, "audit"))
    # A fresh adapter has no service state; both reads must recover persisted metadata.
    repository = DynamoDBCalendarRepository(database, "synthetic")
    assert repository.read_policy_record("pilot").timezone_updated_at == now
    assert repository.read_policy_replay(command).result == result

    for item in database.items.values():
        item.pop("timezone_updated_at", None)
        if "response" in item:
            replay = json.loads(item["response"]["S"])
            replay.pop("timezone_updated_at", None)
            item["response"]["S"] = json.dumps(replay)
    assert repository.read_policy_record("pilot").timezone_updated_at is None
    assert repository.read_policy_replay(command).result.record.timezone_updated_at is None
