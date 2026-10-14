# AWS integration validation

**Scope:** Repository test procedures and dated evidence, reviewed 2026-10-14. A past passing result is not proof for the current deployment. Re-run applicable checks after a separately authorized deployment and record the commit, configuration, date and scope in its issue. These opt-in tests mutate their target and some invoke shared workers; the guards and side effects below matter.

Local tests use synthetic data and a loopback DynamoDB Local endpoint. Deployed tests intentionally retain fixed dev table/queue names because their code refuses other targets. These names describe test constraints, not live resource IDs. Account IDs, function IDs and queue URLs must be resolved privately at run time. Before any deployed test, verify the caller against the approved target account; endpoint checks alone do not verify account identity.

## Transaction and worker validation

Five opt-in tests in `backend/tests/test_dynamodb_local_races.py` exercise actual conditional transactions and the outbox adapter against [AWS DynamoDB Local](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.DownloadingAndRunning.html). Start the official local Java server on a loopback port and run `DYNAMODB_LOCAL_URL=http://127.0.0.1:8013 python -m pytest backend/tests/test_dynamodb_local_races.py`. The fixture creates a unique synthetic table with the two due indexes, then deletes it. It rejects non-loopback endpoints. On DynamoDB Local 3.3.1, approval versus expiry and replacement swap versus an adversarial stale cancellation each produced one committed transaction and one conflict, with no partial audit/outbox write. The cancellation bypasses `LifecycleService`, which would reject it while a replacement guard is active; this second check probes repository atomicity rather than a supported user action. A committed approval also produced one due outbox record that the real DynamoDB adapter handed to a fake queue after bounded polling for local GSI visibility; a fake sender claimed it once and duplicate consumption skipped. No SQS or Twilio traffic occurred. These local results do not prove AWS IAM, deployed GSI propagation timing, SQS/DLQ behavior, or deployed Lambda/EventBridge behavior.

The same test bodies also run against the deployed synthetic table. This mode is opt-in, never runs in CI, and needs the authorization above for each run:

```sh
AWS_PROFILE=scheduling-dev-deployer SCHEDULING_DEV_TABLE=scheduling-dev \
  backend/.venv/bin/python -m pytest -s backend/tests/test_dynamodb_local_races.py
```

The `-s` flag shows the run ID the fixture prints first. The fixture refuses any table other than `scheduling-dev` and any region other than `us-west-1` (`SCHEDULING_DEV_REGION`, if set), fails closed unless the built client's resolved endpoint is exactly `https://dynamodb.us-west-1.amazonaws.com` (this also catches `AWS_ENDPOINT_URL_DYNAMODB` and a profile `endpoint_url`; it does not inspect which account the credentials belong to), never creates or deletes a table, and uses standard AWS credential resolution. Each run uses a unique synthetic business ID and appointment IDs (never `dev-synthetic`) and deletes only the items under those keys afterwards, continuing past per-item errors and failing at the end with the list. Test timestamps are in 2001 so this run's entries sort first in the table-wide due indexes and other records cannot crowd them out of bounded queries. A race where both transactions are cancelled is accepted only if no audit or outbox item exists and the revision is unchanged. It adds two-approvals-for-one-slot and hold-due-index checks, polls the due GSIs for up to 30 seconds for eventual consistency, and runs `DispatchService` through a fake `IntentQueue` scoped to the run's business, so nothing touches SQS, Twilio, or other records. It needs only the `DevTableData` actions (Get, Put, Update, Delete, Query, and the transaction item actions), no `DescribeTable`. A passing run proves IAM, transactions, and GSI visibility on the deployed table, not SQS/DLQ or Lambda behavior.

### Deployed hold-expiry proof

`backend/tests/test_dev_hold_expiry.py` exercises deployed expiry; run with the trigger state reviewed in advance. It reuses the guards and run-scoped cleanup above and invokes the deployed hold-expiry Lambda, so it also needs `lambda:InvokeFunction` on `function:scheduling-dev-*` (the deployer has it). Take the function name from the stack, never an ARN:

```sh
AWS_PROFILE=scheduling-dev-deployer SCHEDULING_DEV_TABLE=scheduling-dev \
  SCHEDULING_DEV_HOLD_EXPIRY_FUNCTION=scheduling-dev-<hold-expiry-function-name> \
  backend/.venv/bin/python -m pytest -s backend/tests/test_dev_hold_expiry.py
```

The function name must start with `scheduling-dev-`, and the Lambda client must resolve to `https://lambda.us-west-1.amazonaws.com`. Because that prefix matches every stack Lambda (the outbox dispatcher sends to SQS, and the retention workers purge data), the test first calls `lambda:GetFunctionConfiguration` and refuses to invoke unless the handler is `scheduling.workers.expiry.expire_due_handler` and its `SCHEDULING_TABLE_NAME` is `scheduling-dev`. The test creates four pending holds for the unique synthetic run business through the real `HoldService` (so the `HoldDueIndex` keys are production-shaped, with 2001 expiry times), then makes one of them stale by marking its appointment `CONFIRMED` while its due-index keys remain. It invokes the function, up to five times because each invocation handles one bounded page, and asserts: the three due holds are `EXPIRED` (version 2, due keys removed) with one `expire` audit record and one `PENDING` client outbox intent each; the stale hold and its items are unchanged; every report has `examined == expired + stale`; and every outbox item of the run is still `PENDING` with zero attempts, so nothing dispatched. The test does not read SQS or Twilio; it relies on outbox dispatch staying disabled, which this test requires. **Side effect:** the worker expires every due hold in the table, not only this run's, including any `dev-synthetic` holds, and each one leaves a `PENDING` client `expire` intent for later outbox dispatch. List due `dev-synthetic` holds first if that matters. The test creates the stale hold first so it sorts ahead of the due holds in `HoldDueIndex`, and each hold ID is printed as `Synthetic hold created: APPOINTMENT#<uuid>` (visible with `-s`). The function role's Get, Put, Update, Delete, ConditionCheck and Query grants are enough; the test needs no other IAM. Without `SCHEDULING_DEV_TABLE`, the same assertions run the handler in-process against DynamoDB Local when `DYNAMODB_LOCAL_URL` is set. Cleanup removes only the run's keys; the sweep below applies.

If a run is killed or cleanup reports errors, sweep leftovers with the deployer profile. Keys are `BUSINESS#synthetic-run-<id>` and `APPOINTMENT#run-<id>-*`; the printed run ID narrows it. List first, then delete each listed key with `delete-item`:

```sh
AWS_PROFILE=scheduling-dev-deployer aws dynamodb scan --table-name scheduling-dev --region us-west-1 \
  --projection-expression "PK, SK" \
  --filter-expression "begins_with(PK, :b) OR begins_with(PK, :a)" \
  --expression-attribute-values '{":b":{"S":"BUSINESS#synthetic-run-"},":a":{"S":"APPOINTMENT#run-"}}'
```

Hold items created by `test_dev_hold_expiry.py` live at `APPOINTMENT#<uuid>` (random IDs), so the scan above misses their META items, which still carry `hold_due_pk = HOLD#PENDING` and a 2001 `hold_due_sk`. After a killed run, find them by business, list first, then delete each META key with `delete-item` (the printed hold IDs narrow it):

```sh
AWS_PROFILE=scheduling-dev-deployer aws dynamodb scan --table-name scheduling-dev --region us-west-1 \
  --projection-expression "PK, SK, business_id" \
  --filter-expression "begins_with(business_id, :b) AND SK = :m" \
  --expression-attribute-values '{":b":{"S":"synthetic-run-"},":m":{"S":"META"}}'
```

### Outbox SQS and DLQ proof

`backend/tests/test_dev_outbox_queue.py` is the gate before the outbox dispatch schedule is ever enabled (issue #87). Its role-stack change set (`OutboxQueueHarness` and `OutboxDeadLetterQueueHarness` in `infra/dev-deploy-roles.yaml`) must be applied first. Run it only with separate authorization:

```sh
AWS_PROFILE=scheduling-dev-deployer SCHEDULING_DEV_TABLE=scheduling-dev \
  SCHEDULING_DEV_OUTBOX_QUEUE=scheduling-outbox-dev \
  backend/.venv/bin/python -m pytest -s backend/tests/test_dev_outbox_queue.py
```

`SCHEDULING_DEV_TABLE` alone never touches SQS; the queue variable must also be exactly `scheduling-outbox-dev`. The #80 guards apply (table, region, DynamoDB endpoint, no CI, unique `synthetic-run-...` business, run ID printed with `-s`), plus the SQS client must resolve to `https://sqs.us-west-1.amazonaws.com` and both queue URLs must match `.../<12-digit account>/scheduling-outbox-dev` and `.../scheduling-outbox-dlq-dev` exactly. The test refuses to start unless the outbox queue redrives to the `scheduling-outbox-dlq-dev` ARN with `maxReceiveCount` between 1 and 10, and both queues are empty (wait for approximate counts to drain after an earlier run).

**Precondition, checked before the first send and again before the poison step (fail closed, including on lookup errors):** the `SmsSenderFunctionOutbox` event source mapping must be `Disabled` and the outbox dispatch rule (`OutboxDispatchFunctionSweep`) must be `DISABLED`. The test finds both through `cloudformation:DescribeStackResource` on stack `scheduling-dev`, then `lambda:GetEventSourceMapping` (it must read `scheduling-outbox-dev`) and `events:DescribeRule`. The deployer already has all three reads (`DeployerDeployPolicy` and `DeployerOperatePolicy`), so no IAM is added for this. Without it, re-running the gate after the sender is enabled would let the real `SmsSenderFunction` consume the run's messages and construct a Twilio client; "never invokes the real sender" holds only under this check.

What it does, with only the run's records:

1. Commits a synthetic approval, so one `PENDING` outbox intent exists. The real `DispatchService` and `SQSIntentQueue` send it to the deployed queue. It does not invoke `OutboxDispatchFunction`, which is table-wide and would also dispatch any `dev-synthetic` intents onto a queue with no consumer. The dispatch store is scoped to the run's business, and the queue wrapper refuses any other business. The dispatch clock is fixed in 2001, like #80, so the run's due-index entries sort first.
2. Receives with the deployer and consumes through the production `consume_sqs_batch` and `ConsumeService` with a fake sender that records deliveries and never constructs a Twilio client. The real `SmsSenderFunction` is never invoked and its mapping stays disabled. A transient failure must leave the record `RETRYABLE` with one attempt and no delivery. A second dispatch plus a deliberately duplicated message must yield one `SENT` delivery, a skipped duplicate, and a skipped redelivery of a message consumed but not deleted (its `ApproximateReceiveCount` one higher than on the previous receive). The DynamoDB outbox record, not the queue, is checked as authoritative.
3. Sends one poison body for the run (`outbox_id` empty). It is received `maxReceiveCount` times without deletion, each time with `VisibilityTimeout=3`, so the 180 second queue default is never waited out; the move to the DLQ happens on the next receive attempt. The test then reads the DLQ record, asserts it is the poison body and that decoding fails, asserts the outbox record and deliveries are unchanged and nothing returned to the main queue, and deletes it. The harness never redrives or replays. Expect roughly 30 to 60 seconds in total.
4. Cleanup receives from both queues and deletes only messages whose `business_id` is this run's; it never purges. A message that is not the run's stops the test and is reported, not deleted. Cleanup reads queue counts first and does not receive from an empty queue. A foreign message is unavoidably received once (its receive count rises by one and it is hidden for a few seconds, since `ChangeMessageVisibility` is not granted) before it can be recognized, so the empty-queue start guard and the precondition above are the real protection; stop and inspect if the test reports one. A leftover is reported with the run ID; remove it by hand with a receipt handle, not a purge.

Without the opt-in the scenario runs against an in-memory fake SQS (visibility timeouts, receive counts, redrive) in `test_fake_queue_path_runs_in_memory_without_dynamodb`, and against DynamoDB Local with the same fake when `DYNAMODB_LOCAL_URL` is set. A passing deployed run proves SQS IAM, dispatch and consumption, duplicate skipping, and DLQ redrive; it does not prove the `SmsSenderFunction` event source mapping, real-provider behavior, or alarms (`OutboxDlqAlarm` will fire on the DLQ record; expect that email, or check the alarm history).

Delete these leftovers before enabling the hold-expiry schedule: a leftover stale entry would be counted by every sweep, and a leftover pending hold would be expired into an already swept partition.

### Deployed retention proof

`backend/tests/test_dev_retention.py` exercises synthetic expiration and legal holds before enabling the retention schedules. It invokes both deployed retention Lambdas (needing `lambda:InvokeFunction` and `lambda:GetFunctionConfiguration`, which the deployer has on `function:scheduling-dev-*`, plus the `DevTableData` Query, Scan, Get, Put, Update and Delete grants (the runtime completed-visit lookup now uses a strongly consistent query); no IAM change is needed). Take the function names from the stack, never ARNs:

```sh
AWS_PROFILE=scheduling-dev-deployer SCHEDULING_DEV_TABLE=scheduling-dev \
  SCHEDULING_DEV_NOTE_RETENTION_FUNCTION=scheduling-dev-<note-retention-function-name> \
  SCHEDULING_DEV_SMS_RETENTION_FUNCTION=scheduling-dev-<sms-retention-function-name> \
  backend/.venv/bin/python -m pytest -s backend/tests/test_dev_retention.py
```

It applies the same table, region, endpoint and CI guards as the race tests, and before any invoke it calls `GetFunctionConfiguration` and refuses unless the handler is exactly `scheduling.workers.note_retention.handler` or `scheduling.workers.sms_retention.handler` (matching the variable), `SCHEDULING_TABLE_NAME` is `scheduling-dev` and `BUSINESS_ID` is `dev-synthetic`. Nothing touches SQS or Twilio.

**Shared business.** Both workers purge the whole `BUSINESS_ID` partition, which is `dev-synthetic`, the owner's live test business. The test therefore never scrubs that partition. It seeds only under a run-specific client `synthetic-run-<id>`, SMS provider IDs `run-<id>-*`, and seven fictional `+1…555 01xx` phone numbers (all printed with `-s`), and its cleanup deletes only those keys, continuing past errors. Before seeding it reads every item in `BUSINESS#dev-synthetic` and **stops without seeding or invoking** if any existing record would be purged: an unheld note past its 12-month clock (using the client's last completed visit, as the worker does), an SMS body whose thread's last exchange is over 90 days old (for an owner message, whose own receipt is over 90 days old), or unheld consent/STOP evidence over four years old. If it stops, the owner's test data has aged out; hold or refresh those records first. Expiries in this precheck are evaluated one day ahead of the local clock, so clock skew cannot hide an item. The run keys (phones, client) are chosen only if no existing item uses them, else the run fails closed, and cleanup refuses to delete any key that existed before the run. After the run it asserts every pre-existing item is unchanged. **Do not use the dev owner app or text the dev number during a run.** The precheck is a point-in-time read: removing a legal hold from an owner note mid-run could get that note deleted, and the snapshot check would only report it afterwards.

**Seeded records and expected results.** Notes (clocks follow `doc/PRD.md` and `note_expired`; the run client has no completed visit, so the clock is the creation date): an expired ordinary note is deleted, an expired note under legal hold is kept, a note created yesterday is kept; the invocation returns `{"deleted_notes": 1}`. SMS: a body whose last exchange was in 2001 has its body removed (receipt metadata stays), a recent body stays, consent evidence recorded in 2001 is deleted (a history row plus the current row), recent evidence and 2001 evidence under a legal hold stay; the invocation returns `{"deleted_sms_bodies": 1, "deleted_sms_evidence": 2}`. Legal hold is exercised for notes, consent evidence, and an SMS body (the domain has no API to hold a body, so the test sets the hold attribute the purge checks). STOP/opt-out evidence is not seeded. Without `SCHEDULING_DEV_TABLE`, the same assertions run the handlers in-process against DynamoDB Local (`DYNAMODB_LOCAL_URL`), with stand-in owner records that must survive.

**Leftover sweep.** If a run is killed or cleanup reports errors, list, then delete each key with `delete-item`, using the printed client ID, provider prefix and phones. Never delete other keys in this partition:

```sh
AWS_PROFILE=scheduling-dev-deployer aws dynamodb query --table-name scheduling-dev --region us-west-1 \
  --projection-expression "PK, SK" --consistent-read \
  --key-condition-expression "PK = :pk" \
  --filter-expression "contains(SK, :c) OR contains(SK, :p) OR contains(SK, :ph)" \
  --expression-attribute-values '{":pk":{"S":"BUSINESS#dev-synthetic"},":c":{"S":"synthetic-run-<id>"},":p":{"S":"SMS#run-<id>-"},":ph":{"S":"<printed phone>"}}'
```

Repeat the filter for each printed phone (`SMS_THREAD#`, `SMS_CONSENT#`, `SMS_CONSENT_CURRENT#`, `PHONE#` keys) and delete the client's `NOTE#CLIENT#<sha256 of the client ID>#*` notes, which carry the client ID only in an attribute: add `client_id = :c` to a filter on the `NOTE#` prefix. Clear leftovers before enabling the retention schedules, because a leftover expired record is otherwise counted or deleted by the first scheduled run.

Use a synthetic `dev` stack after separate provisioning authorization. Repeat the transaction races in AWS, including two simultaneous approvals for one slot; the expected result is at most one committed reservation, with a conflict or current terminal state and no partial outbox event for losing commands. Check SQS duplicate delivery, worker retry and DLQ redrive with a fake sender, then attach an explicitly authorized SMS test number only after Twilio campaign/number approval and the consent/STOP gates. Verify scheduled note and SMS purge with synthetic expired records and legal holds before onboarding real records. No live AWS integration results are claimed by this document.
