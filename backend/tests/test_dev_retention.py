"""Opt-in proof that the note-retention and SMS-retention workers purge only expired synthetic data.

Dev mode invokes the deployed ``scheduling-dev`` Lambdas. With only ``DYNAMODB_LOCAL_URL`` set,
the same assertions run the real handlers in-process against DynamoDB Local.

Both workers act on the whole ``BUSINESS_ID`` partition (``dev-synthetic``, the owner's live
test business), so this test never scrubs that partition. It seeds only under a run-specific
client and run-specific phone numbers, refuses to run if any existing item there could be
purged, snapshots every pre-existing item, asserts the snapshot is unchanged afterwards, and
deletes only the items it owns.
"""

import json
import os
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from test_dev_hold_expiry import FUNCTION_PATTERN, FUNCTION_PREFIX, LAMBDA_ENDPOINT
from test_dynamodb_local_races import DEV_REGION, RaceEnv, _dev_env, _local_env

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore, _four_year_cutoff, _instant
from scheduling.domain.client_records import (
    ClientNote,
    ClientProfile,
    ClientRecordService,
    HomeSize,
    note_expired,
)
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole
from scheduling.workers.note_retention import handler as note_handler
from scheduling.workers.sms_retention import handler as sms_handler

BUSINESS = "dev-synthetic"
NOTE_HANDLER = "scheduling.workers.note_retention.handler"
SMS_HANDLER = "scheduling.workers.sms_retention.handler"
NOTE_FUNCTION_VAR = "SCHEDULING_DEV_NOTE_RETENTION_FUNCTION"
SMS_FUNCTION_VAR = "SCHEDULING_DEV_SMS_RETENTION_FUNCTION"
OLD = datetime(2001, 9, 10, 12, tzinfo=UTC)
BYSTANDER_PHONE = "+12065550100"
EVIDENCE_PREFIXES = ("SMS_CONSENT#", "SMS_CONSENT_CURRENT#", "SMS_OPTOUT#", "SMS_OPTOUT_EVENT#")
Invoke = Callable[[], dict[str, Any]]
Item = dict[str, Any]
Key = tuple[str, str]


def _function_name(variable: str) -> str:
    name = os.environ.get(variable, "")
    if not name.startswith(FUNCTION_PREFIX) or not FUNCTION_PATTERN.fullmatch(name):
        raise ValueError(
            f"{variable} must be a function name starting with {FUNCTION_PREFIX!r} "
            "(not an ARN or another function)"
        )
    return name


def _require_retention_function(configuration: dict[str, Any], handler: str) -> None:
    """Refuse any function that is not the expected retention worker on the dev table.

    Both workers purge the BUSINESS_ID partition, so a function pointed at another business
    or table must never be invoked.
    """
    variables = configuration.get("Environment", {}).get("Variables", {})
    if configuration.get("Handler") != handler:
        raise ValueError(f"Function handler is {configuration.get('Handler')!r}, not {handler!r}")
    if variables.get("SCHEDULING_TABLE_NAME") != "scheduling-dev":
        raise ValueError(
            f"Function SCHEDULING_TABLE_NAME is {variables.get('SCHEDULING_TABLE_NAME')!r}, "
            "not 'scheduling-dev'"
        )
    if variables.get("BUSINESS_ID") != BUSINESS:
        raise ValueError(f"Function BUSINESS_ID is {variables.get('BUSINESS_ID')!r}, not {BUSINESS!r}")


def _lambda_invoker(name: str, handler: str) -> Invoke:
    client = boto3.client("lambda", region_name=DEV_REGION)
    if client.meta.endpoint_url != LAMBDA_ENDPOINT:
        raise ValueError(f"Unexpected Lambda endpoint {client.meta.endpoint_url!r}")
    _require_retention_function(client.get_function_configuration(FunctionName=name), handler)

    def invoke() -> dict[str, Any]:
        response = client.invoke(FunctionName=name, InvocationType="RequestResponse", Payload=b"{}")
        body = response["Payload"].read()
        assert response["StatusCode"] == 200
        assert "FunctionError" not in response, body[:500]
        result = json.loads(body)
        assert isinstance(result, dict)
        print(f"retention report: {json.dumps(result, sort_keys=True)}", flush=True)
        return result

    return invoke


def _partition(client: Any, table: str) -> dict[Key, Item]:
    items: dict[Key, Item] = {}
    last_key: Item | None = None
    while True:
        arguments: dict[str, Any] = {
            "TableName": table, "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": f"BUSINESS#{BUSINESS}"}},
            "ConsistentRead": True,
        }
        if last_key:
            arguments["ExclusiveStartKey"] = last_key
        page = client.query(**arguments)
        for item in page["Items"]:
            items[(item["PK"]["S"], item["SK"]["S"])] = item
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items


@dataclass
class RetentionRun:
    """The synthetic identifiers one run owns inside the shared dev-synthetic business."""

    env: RaceEnv
    invoke: Invoke
    client_id: str
    provider_prefix: str
    phones: list[str] = field(default_factory=list)

    def owns(self, sort_key: str) -> bool:
        client_hash = sha256(self.client_id.encode()).hexdigest()
        if (sort_key == f"CLIENT#{self.client_id}"
                or sort_key.startswith((self.provider_prefix, f"NOTE#CLIENT#{client_hash}#"))):
            return True
        for phone in self.phones:
            if sort_key in {f"PHONE#{phone}", f"SMS_THREAD#{phone}", f"SMS_SUPPRESS#{phone}",
                            f"SMS_CONSENT_CURRENT#{phone}", f"SMS_OPTOUT#{phone}"}:
                return True
            if sort_key.startswith((f"SMS_CONSENT#{phone}#", f"SMS_OPTOUT_EVENT#{phone}#")):
                return True
        return False

    def owned_items(self) -> dict[Key, Item]:
        return {key: item for key, item in _partition(self.env.client, self.env.table).items()
                if self.owns(key[1])}

    def others(self) -> dict[Key, Item]:
        return {key: item for key, item in _partition(self.env.client, self.env.table).items()
                if not self.owns(key[1])}

    def cleanup(self) -> None:
        """Delete only this run's items, continuing past errors and reporting at the end."""
        errors: list[str] = []
        try:
            owned = self.owned_items()
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"Cleanup for {self.client_id} could not list items: {exc}") from exc
        for pk, sk in owned:
            try:
                self.env.client.delete_item(
                    TableName=self.env.table, Key={"PK": {"S": pk}, "SK": {"S": sk}})
            except (BotoCoreError, ClientError) as exc:
                errors.append(f"delete {sk}: {exc}")
        if errors:
            raise RuntimeError(
                f"Cleanup for {self.client_id} left items behind; sweep them by key: "
                + "; ".join(errors))


def purge_risks(items: dict[Key, Item], now: datetime,
                last_visit_end: Callable[[str], datetime | None]) -> list[str]:
    """Name each existing item that a retention run would delete (conservative).

    SMS evidence ignores the thread check, so an item that might survive is still reported.
    """
    cutoff = _four_year_cutoff(now)
    body_cutoff = _instant(now - timedelta(days=90))
    risks: list[str] = []
    for (_, sk), item in items.items():
        if sk.startswith("NOTE#") and "legal_hold_reason" not in item:
            note = ClientNote(
                item["business_id"]["S"], item["client_id"]["S"], item["note_id"]["S"], None,
                item["body"]["S"], item["created_by"]["S"],
                datetime.fromisoformat(item["created_at"]["S"]))
            if note_expired(note, last_visit_end(note.client_id), now):
                risks.append(f"note {sk}")
        elif sk.startswith("SMS#") and ("body" in item or "reply_text" in item):
            thread = items.get((f"BUSINESS#{BUSINESS}", f"SMS_THREAD#{item['sender']['S']}"))
            if ("legal_hold_reason" not in item and thread is not None
                    and thread["last_exchange_at"]["S"] <= body_cutoff):
                risks.append(f"sms body {sk}")
        elif sk.startswith(EVIDENCE_PREFIXES) and "legal_hold_reason" not in item:
            field_name = "opted_out_at" if sk.startswith("SMS_OPTOUT") else "agreed_at"
            if item.get(field_name, {}).get("S", "") <= cutoff:
                risks.append(f"sms evidence {sk}")
    return risks


def _profile(client_id: str, phone: str, now: datetime) -> ClientProfile:
    return ClientProfile(BUSINESS, client_id, "Synthetic Retention Client", phone,
                         "1 Synthetic Way", HomeSize.SMALL, 60, True, 1, now, now)


def _phone(rng: random.Random) -> str:
    return f"+1{rng.randint(200, 999)}55501{rng.randint(0, 99):02d}"  # fictional 555-01xx block


def _seed_bystanders(env: RaceEnv, now: datetime) -> None:
    """Local mode only: stand in for the owner's existing, non-expired test records."""
    repo = DynamoDBCalendarRepository(env.client, env.table)
    store = DynamoSmsIngressStore(env.client, env.table)
    repo.save_profile(_profile("owner-test-client", BYSTANDER_PHONE, now), 0, None)
    repo.put_note(ClientNote(BUSINESS, "owner-test-client", "owner-note", None,
                             "Synthetic owner note", "synthetic-owner", now))
    store.put_consent(ConsentEvidence(BUSINESS, "owner-test-client", "Synthetic Owner",
                                      BYSTANDER_PHONE, now, "synthetic-script"))
    store.put_received(InboundReceipt(
        BUSINESS, "owner-sms", BYSTANDER_PHONE, "+12065550101", "synthetic owner text", now,
        SenderRole.CLIENT, "owner-test-client", Keyword.OTHER, True))


def _retention_run(function_var: str, handler_path: str, in_process: Invoke,
                   monkeypatch: pytest.MonkeyPatch) -> Iterator[RetentionRun]:
    if os.environ.get("SCHEDULING_DEV_TABLE"):
        for env in _dev_env():  # table, region, endpoint guards, CI skip, run id
            invoke = _lambda_invoker(_function_name(function_var), handler_path)
            yield from _prepared(env, invoke)
        return
    for env in _local_env():
        url = os.environ["DYNAMODB_LOCAL_URL"]
        monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", url)
        monkeypatch.setenv("AWS_DEFAULT_REGION", DEV_REGION)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic")
        monkeypatch.setenv("SCHEDULING_TABLE_NAME", env.table)
        monkeypatch.setenv("BUSINESS_ID", BUSINESS)
        _seed_bystanders(env, datetime.now(UTC))
        yield from _prepared(env, in_process)


def _prepared(env: RaceEnv, invoke: Invoke) -> Iterator[RetentionRun]:
    hex_id = env.run.removeprefix("run-")
    run = RetentionRun(env, invoke, f"synthetic-run-{hex_id}", f"SMS#{env.run}-")
    rng = random.Random(hex_id)
    while len(run.phones) < 6:
        phone = _phone(rng)
        if phone not in run.phones and phone != BYSTANDER_PHONE:
            run.phones.append(phone)
    print(f"Synthetic retention run {env.run}: business {BUSINESS} (shared, never scrubbed), "
          f"client {run.client_id}, SMS provider prefix {run.provider_prefix}, "
          f"phones {', '.join(run.phones)}", flush=True)
    try:
        yield run
    finally:
        run.cleanup()


@pytest.fixture
def note_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[RetentionRun]:
    yield from _retention_run(NOTE_FUNCTION_VAR, NOTE_HANDLER,
                              lambda: note_handler({}, None), monkeypatch)


@pytest.fixture
def sms_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[RetentionRun]:
    yield from _retention_run(SMS_FUNCTION_VAR, SMS_HANDLER,
                              lambda: sms_handler({}, None), monkeypatch)


def _precheck(run: RetentionRun, now: datetime) -> dict[Key, Item]:
    """Stop before seeding unless nothing already in dev-synthetic could be purged."""
    before = _partition(run.env.client, run.env.table)
    assert not [key for key in before if run.owns(key[1])], "Run keys already exist"
    repo = DynamoDBCalendarRepository(run.env.client, run.env.table)
    risks = purge_risks(before, now, lambda client_id: repo.last_visit_end(BUSINESS, client_id, now))
    if risks:
        pytest.fail("Refusing to run: the retention workers would also delete existing "
                    f"{BUSINESS} records (nothing was seeded or invoked): " + "; ".join(risks))
    return before


def test_note_retention_worker_deletes_only_expired_unheld_notes(note_run: RetentionRun) -> None:
    run = note_run
    now = datetime.now(UTC)
    before = _precheck(run, now)
    repo = DynamoDBCalendarRepository(run.env.client, run.env.table)
    service = ClientRecordService(repo)
    repo.save_profile(_profile(run.client_id, run.phones[0], now), 0, None)

    def note(note_id: str, created: datetime) -> ClientNote:
        item = ClientNote(BUSINESS, run.client_id, f"{run.env.run}-{note_id}", None,
                          f"Synthetic {note_id} note", "synthetic-run", created)
        repo.put_note(item)  # put_note skips create_note's expiry rejection, to seed old notes
        return item

    expired = note("expired", OLD)
    held = note("held", OLD)
    service.change_note_hold(BUSINESS, run.client_id, held.note_id, "synthetic legal hold")
    current = note("current", now - timedelta(days=1))
    # No completed visit exists for the run client, so each note's clock is its creation date.
    assert repo.last_visit_end(BUSINESS, run.client_id, now) is None

    assert run.invoke() == {"deleted_notes": 1}

    assert repo.read_note(BUSINESS, run.client_id, expired.note_id) is None
    kept_held = repo.read_note(BUSINESS, run.client_id, held.note_id)
    assert kept_held is not None and kept_held.legal_hold_reason == "synthetic legal hold"
    assert repo.read_note(BUSINESS, run.client_id, current.note_id) == current
    assert repo.read_profile(BUSINESS, run.client_id) is not None
    assert run.others() == before


def test_sms_retention_worker_purges_expired_bodies_and_keeps_held_or_current_evidence(
    sms_run: RetentionRun,
) -> None:
    run = sms_run
    now = datetime.now(UTC)
    before = _precheck(run, now)
    store = DynamoSmsIngressStore(run.env.client, run.env.table)
    old_body, new_body, old_evidence, new_evidence, held_evidence = run.phones[1:]
    yesterday = now - timedelta(days=1)

    def receive(name: str, phone: str, received: datetime) -> str:
        provider_id = f"{run.env.run}-{name}"
        assert store.put_received(InboundReceipt(
            BUSINESS, provider_id, phone, "+12065550199", f"Synthetic {name} text", received,
            SenderRole.CLIENT, run.client_id, Keyword.OTHER, True))
        return provider_id

    def consent(phone: str, agreed: datetime) -> None:
        store.put_consent(ConsentEvidence(
            BUSINESS, run.client_id, "Synthetic Participant", phone, agreed, "synthetic-script"))

    old_id = receive("body-old", old_body, OLD)
    new_id = receive("body-new", new_body, yesterday)
    consent(old_evidence, OLD)
    consent(new_evidence, yesterday)
    consent(held_evidence, OLD)
    history_keys = [f"SMS_CONSENT#{phone}#{_instant(when)}" for phone, when in (
        (old_evidence, OLD), (new_evidence, yesterday), (held_evidence, OLD))]
    current_keys = [f"SMS_CONSENT_CURRENT#{phone}" for phone in
                    (old_evidence, new_evidence, held_evidence)]
    for sort_key in (history_keys[2], current_keys[2]):
        store.set_evidence_legal_hold(BUSINESS, sort_key, "synthetic legal hold")

    # Consent evidence is one history row plus one current row, so the expired phone has two.
    assert run.invoke() == {"deleted_sms_bodies": 1, "deleted_sms_evidence": 2}

    def read(sort_key: str) -> Item | None:
        return run.env.client.get_item(TableName=run.env.table, ConsistentRead=True, Key={
            "PK": {"S": f"BUSINESS#{BUSINESS}"}, "SK": {"S": sort_key}}).get("Item")

    purged = read(f"SMS#{old_id}")
    assert purged is not None and "body" not in purged and purged["sender"]["S"] == old_body
    kept = read(f"SMS#{new_id}")
    assert kept is not None and kept["body"]["S"] == "Synthetic body-new text"
    assert read(history_keys[0]) is None and read(current_keys[0]) is None
    for sort_key in (history_keys[1], current_keys[1]):
        assert read(sort_key) is not None
    for sort_key in (history_keys[2], current_keys[2]):
        held = read(sort_key)
        assert held is not None and held["legal_hold_reason"]["S"] == "synthetic legal hold"
    assert run.others() == before


def test_precheck_reports_existing_records_a_purge_would_delete() -> None:
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    pk = {"S": f"BUSINESS#{BUSINESS}"}

    def item(sk: str, **fields: str) -> tuple[Key, Item]:
        return (pk["S"], sk), {"PK": pk, "SK": {"S": sk}, **{k: {"S": v} for k, v in fields.items()}}

    def note(sk: str, created: datetime, **extra: str) -> tuple[Key, Item]:
        return item(sk, business_id=BUSINESS, client_id="c", note_id="n", body="b",
                    created_by="a", created_at=_instant(created), **extra)

    def no_visit(_client: str) -> datetime | None:
        return None

    safe = dict([
        note("NOTE#CLIENT#x#fresh", now - timedelta(days=30)),
        note("NOTE#CLIENT#x#held", OLD, legal_hold_reason="hold"),
        item("SMS#fresh", sender="+12065550100", body="b"),
        item("SMS_THREAD#+12065550100", last_exchange_at=_instant(now)),
        item("SMS_CONSENT#+12065550100#t", agreed_at=_instant(now)),
        item("SMS_CONSENT_CURRENT#+12065550101", agreed_at=_instant(OLD), legal_hold_reason="h"),
    ])
    assert purge_risks(safe, now, no_visit) == []
    # A later completed visit keeps an old note alive, exactly as the worker's clock does.
    old_note = dict([note("NOTE#CLIENT#x#old", OLD)])
    assert purge_risks(old_note, now, no_visit) == ["note NOTE#CLIENT#x#old"]
    assert purge_risks(old_note, now, lambda _client: now - timedelta(days=1)) == []
    risky = dict([
        item("SMS#old", sender="+12065550102", body="b"),
        item("SMS_THREAD#+12065550102", last_exchange_at=_instant(OLD)),
        item("SMS_CONSENT#+12065550103#t", agreed_at=_instant(OLD)),
        item("SMS_OPTOUT#+12065550104", opted_out_at=_instant(OLD)),
    ])
    assert sorted(purge_risks(risky, now, no_visit)) == sorted([
        "sms body SMS#old", "sms evidence SMS_CONSENT#+12065550103#t",
        "sms evidence SMS_OPTOUT#+12065550104"])


def test_run_ownership_covers_only_run_keys() -> None:
    run = RetentionRun(None, dict,"synthetic-run-abc", "SMS#run-abc-",  # type: ignore[arg-type]
                       ["+12065550142"])
    owned = ["CLIENT#synthetic-run-abc", "PHONE#+12065550142", "SMS#run-abc-body-old",
             "SMS_THREAD#+12065550142", "SMS_CONSENT_CURRENT#+12065550142",
             "SMS_CONSENT#+12065550142#2001-09-10T12:00:00.000000+00:00",
             f"NOTE#CLIENT#{sha256(b'synthetic-run-abc').hexdigest()}#n1"]
    foreign = ["CLIENT#owner-test-client", "PHONE#+1206555014", "PHONE#+120655501422",
               "SMS#run-abd-body-old", "SMS#other", "SMS_THREAD#+1206555014",
               "SMS_CONSENT#+120655501422#t", "POLICY#SCHEDULING", "CALENDAR#REVISION",
               f"NOTE#CLIENT#{sha256(b'owner-test-client').hexdigest()}#n1"]
    assert all(run.owns(key) for key in owned)
    assert not any(run.owns(key) for key in foreign)


def test_function_guard_refuses_other_handlers_tables_and_businesses() -> None:
    def configuration(handler: str = NOTE_HANDLER, **changes: str) -> dict[str, Any]:
        return {"Handler": handler, "Environment": {"Variables": {
            "SCHEDULING_TABLE_NAME": "scheduling-dev", "BUSINESS_ID": BUSINESS, **changes}}}

    _require_retention_function(configuration(), NOTE_HANDLER)
    _require_retention_function(configuration(SMS_HANDLER), SMS_HANDLER)
    with pytest.raises(ValueError, match="handler"):
        _require_retention_function(configuration(), SMS_HANDLER)  # swapped function variables
    with pytest.raises(ValueError, match="handler"):
        _require_retention_function(
            configuration("scheduling.workers.outbox.dispatch_due_handler"), NOTE_HANDLER)
    with pytest.raises(ValueError, match="handler"):
        _require_retention_function({}, NOTE_HANDLER)
    with pytest.raises(ValueError, match="SCHEDULING_TABLE_NAME"):
        _require_retention_function(configuration(SCHEDULING_TABLE_NAME="other"), NOTE_HANDLER)
    with pytest.raises(ValueError, match="BUSINESS_ID"):
        _require_retention_function(configuration(BUSINESS_ID="pilot-business"), NOTE_HANDLER)
    with pytest.raises(ValueError, match="BUSINESS_ID"):
        _require_retention_function({"Handler": NOTE_HANDLER, "Environment": {"Variables": {
            "SCHEDULING_TABLE_NAME": "scheduling-dev"}}}, NOTE_HANDLER)


def test_function_name_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NOTE_FUNCTION_VAR, "scheduling-dev-NoteRetention-abc")
    assert _function_name(NOTE_FUNCTION_VAR) == "scheduling-dev-NoteRetention-abc"
    for bad in ("", "other-fn", "arn:aws:lambda:us-west-1:1:function:scheduling-dev-x"):
        monkeypatch.setenv(NOTE_FUNCTION_VAR, bad)
        with pytest.raises(ValueError, match="function name"):
            _function_name(NOTE_FUNCTION_VAR)
