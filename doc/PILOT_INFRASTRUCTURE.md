# Pilot infrastructure plan (code only)

Issue [#23](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/23) targets `us-west-1`. The synthetic `dev` stack goes in the dedicated member account `214965372605`; the `pilot` stack, which will hold real client data, is planned for its own separate account (see [ARCHITECTURE.md](ARCHITECTURE.md)). The `template.yaml` stack is a reviewable foundation for the authenticated owner calendar. It has **not** been deployed; provisioning, owner account creation, live SMS, and customer data onboarding each need a separate authorized step.

## Current stack boundary

- The owner HTTP API exposes only the owner API under `/v1/owner/{proxy+}`; the owner web app is a separate static Amplify app (`frontend/`). API Gateway checks a Cognito JWT on the explicit GET, POST, PUT, PATCH, and DELETE owner API routes; there is deliberately no `ANY` or `OPTIONS` route, so the HTTP API CORS configuration answers browser preflight requests without the authorizer; the FastAPI verifier also requires the exact issuer, client ID, access-token type, owner subject, and business ID. The local synthetic API and Twilio webhooks are not routed by this stack.
- A single on-demand DynamoDB table has the `PK`/`SK` primary key, `HoldDueIndex`, and `OutboxDueIndex` expected by the adapters. Point-in-time recovery is enabled and CloudFormation retains the table on stack deletion or replacement. Deleting a stack therefore does **not** delete customer records.
- Two encrypted SQS Standard queues, each with a dead-letter queue, are defined: one for outbox delivery and one for SMS conversation receipts. The SMS sender has a **disabled** SQS event source and requires `SmsSendEnabled=authorized` plus an explicit recipient allowlist, so deployment alone cannot send a text. Hold expiry, outbox dispatch, note retention, and SMS retention rules are also **disabled**. Operators must only enable them as part of an approved, monitored deployment. In particular, enabling outbox dispatch without its consumer would accumulate messages.
- Lambda logs have 30-day retention. Error alarms cover the API and each worker; each dead-letter queue has a depth alarm. The expiry and outbox workers emit structured JSON with their oldest observed due age, and CloudWatch metric filters alarm when either exceeds 15 minutes. Every alarm publishes to an SNS topic with an email subscription supplied through the required `AlarmEmail` parameter. The mailbox must confirm its subscription before alarms can be relied on. No email address is stored in the repository.
- The Twilio webhook API is conditional and off by default. When enabled, it verifies the exact signed inbound/status URLs and retrieves the auth token from the environment-specific SSM SecureString path `/scheduling/{dev|pilot}/twilio/auth-token` at cold start; a plaintext `String` parameter is rejected. The SQS sender uses the same scoped parameter and refuses to send unless the active ingress API base and both exact callback URLs agree. The token is never in the template or Lambda environment. CloudFormation cannot create the SecureString value. A customer-managed KMS key would need a separately scoped decrypt grant. No token, real phone number, or recipient list belongs in this repository.
- SMS evidence legal-hold management and a GitHub OIDC deploy role remain to be integrated. The SMS conversation layer is in the template (a receipt queue, its dead-letter queue, and a worker) but stays inert: it runs only when SMS ingress is on, `SmsSendEnabled=authorized`, and `EnableSmsConversations=authorized`, and it reads an OpenAI key from a SecureString that CloudFormation cannot create. This foundation does not authorize real messaging.

## Design diagrams

The diagrams show the intended deployed topology and the operational gates around it. They are not evidence that any resource has been provisioned. A dashed line is a path that is conditional or disabled in the initial stack; the labels on those lines name the relevant gate.

### Runtime topology

```mermaid
flowchart LR
    Owner[Business owner] --> App[Amplify static owner app]
    App -->|authorization code + PKCE| Cognito[Cognito hosted UI and user pool]
    App -->|access token| OwnerApi[Owner HTTP API<br/>JWT authorizer]
    OwnerApi -->|GET / POST / PUT / PATCH / DELETE| OwnerFn[Owner API Lambda]
    OwnerFn --> Table[(DynamoDB scheduling table<br/>PK / SK + due indexes + PITR)]

    Customer[Approved SMS recipient] -.->|EnableSmsIngress=true| Twilio[Twilio]
    Twilio -.->|signed inbound and status webhooks| SmsApi[Conditional SMS HTTP API]
    SmsApi -.-> IngressFn[SMS ingress Lambda]
    IngressFn -.-> ReceiptQ[[SMS conversation queue]]
    ReceiptQ -.->|three authorization gates| ConversationFn[SMS conversation Lambda]
    ConversationFn -.-> Table
    ConversationFn -.-> OpenAISecret[SSM SecureString<br/>OpenAI key]

    HoldSchedule[Hold-expiry schedule] -.->|disabled initially| HoldFn[Hold-expiry Lambda]
    HoldFn -->|HoldDueIndex| Table
    OutboxSchedule[Outbox-dispatch schedule] -.->|disabled initially| DispatchFn[Outbox-dispatch Lambda]
    DispatchFn -->|OutboxDueIndex| Table
    DispatchFn --> OutboxQ[[Outbox queue]]
    OutboxQ -.->|event source disabled + send authorization| SenderFn[SMS sender Lambda]
    SenderFn -.-> Twilio

    NoteSchedule[Note-retention schedule] -.->|disabled initially| NoteFn[Note-retention Lambda]
    SmsSchedule[SMS-retention schedule] -.->|disabled initially| SmsRetentionFn[SMS-retention Lambda]
    NoteFn --> Table
    SmsRetentionFn --> Table

    ReceiptQ --> ReceiptDlq[[Conversation DLQ]]
    OutboxQ --> OutboxDlq[[Outbox DLQ]]
    Secrets[SSM SecureString<br/>Twilio auth token] -.-> IngressFn
    Secrets -.-> SenderFn

    Observability[CloudWatch logs, metrics,<br/>error and age alarms] --> AlarmTopic[SNS alarm topic]
    AlarmTopic -->|subscription must be confirmed| Mailbox[Owner-monitored mailbox]
    OwnerFn --> Observability
    IngressFn -.-> Observability
    ConversationFn -.-> Observability
    SenderFn -.-> Observability
    HoldFn --> Observability
    DispatchFn --> Observability
    NoteFn --> Observability
    SmsRetentionFn --> Observability
    ReceiptDlq --> Observability
    OutboxDlq --> Observability
```

The table is the system of record; SQS carries work rather than owning scheduling state. Both queues redrive failed messages to their own DLQ. The diagram groups CloudWatch components to keep the topology readable: each Lambda has an error alarm, each DLQ has a depth alarm, and the hold-expiry and outbox-dispatch logs also feed oldest-due-age metric alarms.

### Deployment and enablement gates

```mermaid
flowchart TD
    Code[Reviewed code only] --> Validate[Validate and build locally]
    Validate --> Identity{Correct account and<br/>us-west-1?}
    Identity -->|no| Stop[Stop: make no account changes]
    Identity -->|yes| Authorization{Explicit provisioning<br/>authorization?}
    Authorization -->|no| Stop
    Authorization -->|yes| Roles[Create and review role-stack change set]
    Roles --> DevChangeSet[Create dev-stack change set<br/>with synthetic-only parameters]
    DevChangeSet --> Review{Owner reviews resources,<br/>cost, mailbox, and rollback}
    Review -->|not approved| Stop
    Review -->|approved| Dev[Execute synthetic dev stack<br/>all schedules and SMS sending off]
    Dev --> OwnerBootstrap[Bootstrap owner callback and subject;<br/>confirm SNS subscription]
    OwnerBootstrap --> Proof[Run authentication, transaction-race,<br/>IAM, alarm, and retention proofs]
    Proof --> Gate{All applicable gates pass?}
    Gate -->|no| Brake[Disable affected trigger,<br/>inspect, and roll back code separately]
    Gate -->|yes| PilotReview[Separate pilot-account,<br/>real-data, and live-SMS reviews]
    PilotReview -->|not separately authorized| DevOnly[Remain synthetic and disconnected]
    PilotReview -->|each action authorized| Incremental[Enable one reviewed capability at a time<br/>and monitor it]
```

Provisioning, real-data onboarding, SMS ingress, conversation processing, and SMS sending are separate decisions. Passing the synthetic `dev` checkpoint does not imply authorization for the `pilot` account or live traffic. In particular, the initial deployment keeps all four schedules and the SMS sender event source disabled; SMS conversation processing additionally requires ingress, sending, and conversation authorization.

## Build and preflight

From the repository root, run `sam validate --lint --template-file template.yaml --region us-west-1` and `sam build --template-file template.yaml`. These commands only validate and build local artifacts. Before any separately authorized deployment, run `aws sts get-caller-identity` and reject any account other than the target for that stack (`214965372605` for `dev`; `pilot` has no account yet, so it is rejected until one is recorded); check the CLI region is `us-west-1`. Use separate `dev` and `pilot` stack names, a unique Cognito domain prefix, and an owner-monitored `AlarmEmail` mailbox. `OwnerAppOrigin` is the exact Amplify owner app origin (`https://host[:port]`, no path or trailing slash). The owner HTTP API's CORS configuration allows only that origin, the owner route methods, and the `Authorization`, `Content-Type`, and `Idempotency-Key` headers, without credentials; API Gateway answers preflight requests itself. The same origin plus `/` is the Cognito callback and logout URL, and the origin reaches the Lambda as `OWNER_APP_ORIGIN`. Until the Amplify app exists, use an inert value such as `https://example.invalid`.

The first stack creation needs placeholders for `OwnerSub` and `OwnerAppOrigin`: the owner subject and the Amplify app origin do not yet exist. Use an inert origin such as `https://example.invalid`, and do not sign in or send any owner API traffic. Create the single owner user. Create the Amplify app with platform `WEB`. For `dev`, the owner app is a manual deploy with no Git connection (owner decision, 2026-09-30, #43): AWS gets no repository access, pushes trigger no builds (no build minutes), and each update is an explicit upload. Create the app with `aws amplify create-app --name scheduling-owner-dev --platform WEB --custom-headers <customHeaders YAML> --region us-west-1`, then `aws amplify create-branch --app-id <app id> --branch-name main --region us-west-1`. Build locally from `frontend/` with `NEXT_PUBLIC_API_BASE_URL` (`OwnerApiUrl`, no trailing slash), `NEXT_PUBLIC_COGNITO_DOMAIN` (`OwnerCognitoDomain`), `NEXT_PUBLIC_COGNITO_CLIENT_ID` (`OwnerAppClientId`), `NEXT_PUBLIC_BUSINESS_ID=dev-synthetic`, and `NEXT_PUBLIC_AUTH_MODE=cognito`, run `npm run check:export`, and zip the contents of `frontend/out`. Deploy with `aws amplify create-deployment --app-id <app id> --branch-name main --region us-west-1` (returns `jobId` and `zipUploadUrl`), upload the zip to `zipUploadUrl` with an HTTP PUT, then `aws amplify start-deployment --app-id <app id> --branch-name main --job-id <job id> --region us-west-1`. A manual app cannot read `customHttp.yml` from a repository, so its headers are applied with `--custom-headers` in the non-monorepo `customHeaders:` format (the `pattern`/`headers` entries of `customHttp.yml` without the `applications`/`appRoot` wrapper); `customHttp.yml` is the source of that value, and their presence is verified after deploy. The origin is `https://main.<app id>.amplifyapp.com`. `amplify.yml` and `customHttp.yml` remain the source for a future Git-connected app, which would be a new Amplify app with a new origin followed by one reviewed `OwnerAppOrigin` change set. For a Git-connected app (for example `pilot`), set `AMPLIFY_MONOREPO_APP_ROOT=frontend` and the build variables on the Amplify app instead. The build variables come from the stack outputs `OwnerApiUrl`, `OwnerCognitoDomain`, and `OwnerAppClientId`, plus `NEXT_PUBLIC_BUSINESS_ID` equal to the stack's `BusinessId` parameter (see `frontend/README.md`). Then update `OwnerAppOrigin` to the exact Amplify origin and `OwnerSub` to that user's Cognito `sub`. Only after this update should the owner sign in through the hosted UI with authorization code and PKCE. Verify that the Amplify response headers from `customHttp.yml` are present, that a browser preflight is answered by API Gateway, that an unauthenticated owner API call fails and whether that 401 carries `Access-Control-Allow-Origin`, and that only that exact subject can read the pilot business calendar. Seed the documented owner policy through the authenticated API before any booking writes. Do not use a broadly shared owner account.

Keep `EnableSmsIngress=false` until its separately authorized setup is complete. Because `SmsApiUrl` exists only after enabling that resource, use a two-step bootstrap: create the SecureString at the exact `/scheduling/${Environment}/twilio/auth-token` path first; enable ingress with the inert `example.invalid` signed URLs while the Twilio number still points nowhere; read `SmsApiUrl`; immediately update both URL parameters to that exact base plus `/webhooks/sms/inbound` and `/webhooks/sms/status`. Then verify invalid signatures fail and a synthetic valid signature works before configuring Twilio's approved number. Keep the SQS trigger disabled and `SmsSendEnabled=disabled` throughout this bootstrap. Do not accept real traffic until the exact URLs, retention schedules, monitoring, consent gates, and separate live-SMS authorization are in place.

## Integration proof still required

Five opt-in tests in `backend/tests/test_dynamodb_local_races.py` exercise actual conditional transactions and the outbox adapter against [AWS DynamoDB Local](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.DownloadingAndRunning.html). Start the official local Java server on a loopback port and run `DYNAMODB_LOCAL_URL=http://127.0.0.1:8013 python -m pytest backend/tests/test_dynamodb_local_races.py`. The fixture creates a unique synthetic table with the two due indexes, then deletes it. It rejects non-loopback endpoints. On DynamoDB Local 3.3.1, approval versus expiry and replacement swap versus an adversarial stale cancellation each produced one committed transaction and one conflict, with no partial audit/outbox write. The cancellation bypasses `LifecycleService`, which would reject it while a replacement guard is active; this second check probes repository atomicity rather than a supported user action. A committed approval also produced one due outbox record that the real DynamoDB adapter handed to a fake queue after bounded polling for local GSI visibility; a fake sender claimed it once and duplicate consumption skipped. No SQS or Twilio traffic occurred. These local results do not prove AWS IAM, deployed GSI propagation timing, SQS/DLQ behavior, or deployed Lambda/EventBridge behavior.

The same test bodies also run against the deployed synthetic table. This mode is opt-in, never runs in CI, and needs the authorization above for each run:

```sh
AWS_PROFILE=scheduling-dev-deployer SCHEDULING_DEV_TABLE=scheduling-dev \
  backend/.venv/bin/python -m pytest -s backend/tests/test_dynamodb_local_races.py
```

The `-s` flag shows the run ID the fixture prints first. The fixture refuses any table other than `scheduling-dev` and any region other than `us-west-1` (`SCHEDULING_DEV_REGION`, if set), fails closed unless the built client's resolved endpoint is exactly `https://dynamodb.us-west-1.amazonaws.com` (this also catches `AWS_ENDPOINT_URL_DYNAMODB` and a profile `endpoint_url`; it does not inspect which account the credentials belong to), never creates or deletes a table, and uses standard AWS credential resolution. Each run uses a unique synthetic business ID and appointment IDs (never `dev-synthetic`) and deletes only the items under those keys afterwards, continuing past per-item errors and failing at the end with the list. Test timestamps are in 2001 so this run's entries sort first in the table-wide due indexes and other records cannot crowd them out of bounded queries. A race where both transactions are cancelled is accepted only if no audit or outbox item exists and the revision is unchanged. It adds two-approvals-for-one-slot and hold-due-index checks, polls the due GSIs for up to 30 seconds for eventual consistency, and runs `DispatchService` through a fake `IntentQueue` scoped to the run's business, so nothing touches SQS, Twilio, or other records. It needs only the `DevTableData` actions (Get, Put, Update, Delete, Query, and the transaction item actions), no `DescribeTable`. A passing run proves IAM, transactions, and GSI visibility on the deployed table, not SQS/DLQ or Lambda behavior.

### Deployed hold-expiry proof

`backend/tests/test_dev_hold_expiry.py` is checkpoint step 5's proof, run before the hold-expiry schedule is enabled. It reuses the guards and run-scoped cleanup above and invokes the deployed hold-expiry Lambda, so it also needs `lambda:InvokeFunction` on `function:scheduling-dev-*` (the deployer has it). Take the function name from the stack, never an ARN:

```sh
AWS_PROFILE=scheduling-dev-deployer SCHEDULING_DEV_TABLE=scheduling-dev \
  SCHEDULING_DEV_HOLD_EXPIRY_FUNCTION=scheduling-dev-<hold-expiry-function-name> \
  backend/.venv/bin/python -m pytest -s backend/tests/test_dev_hold_expiry.py
```

The function name must start with `scheduling-dev-`, and the Lambda client must resolve to `https://lambda.us-west-1.amazonaws.com`. Because that prefix matches every stack Lambda (the outbox dispatcher sends to SQS, and the retention workers purge data), the test first calls `lambda:GetFunctionConfiguration` and refuses to invoke unless the handler is `scheduling.workers.expiry.expire_due_handler` and its `SCHEDULING_TABLE_NAME` is `scheduling-dev`. The test creates four pending holds for the unique synthetic run business through the real `HoldService` (so the `HoldDueIndex` keys are production-shaped, with 2001 expiry times), then makes one of them stale by marking its appointment `CONFIRMED` while its due-index keys remain. It invokes the function, up to five times because each invocation handles one bounded page, and asserts: the three due holds are `EXPIRED` (version 2, due keys removed) with one `expire` audit record and one `PENDING` client outbox intent each; the stale hold and its items are unchanged; every report has `examined == expired + stale`; and every outbox item of the run is still `PENDING` with zero attempts, so nothing dispatched. The test does not read SQS or Twilio; it relies on outbox dispatch staying disabled, which this checkpoint requires. **Side effect:** the worker expires every due hold in the table, not only this run's, including any `dev-synthetic` holds, and each one leaves a `PENDING` client `expire` intent for later outbox dispatch. List due `dev-synthetic` holds first if that matters. The test creates the stale hold first so it sorts ahead of the due holds in `HoldDueIndex`, and each hold ID is printed as `Synthetic hold created: APPOINTMENT#<uuid>` (visible with `-s`). The function role's Get, Put, Update, Delete, ConditionCheck and Query grants are enough; the test needs no other IAM. Without `SCHEDULING_DEV_TABLE`, the same assertions run the handler in-process against DynamoDB Local when `DYNAMODB_LOCAL_URL` is set. Cleanup removes only the run's keys; the sweep below applies.

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

`backend/tests/test_dev_retention.py` is the synthetic expiration and legal-hold check that checkpoint step 5 requires before the note and SMS retention schedules are enabled. It invokes both deployed retention Lambdas (needing `lambda:InvokeFunction` and `lambda:GetFunctionConfiguration`, which the deployer has on `function:scheduling-dev-*`, plus the `DevTableData` Query, Scan, Get, Put, Update and Delete grants (Scan is used by the client's last-visit lookup); no IAM change is needed). Take the function names from the stack, never ARNs:

```sh
AWS_PROFILE=scheduling-dev-deployer SCHEDULING_DEV_TABLE=scheduling-dev \
  SCHEDULING_DEV_NOTE_RETENTION_FUNCTION=scheduling-dev-<note-retention-function-name> \
  SCHEDULING_DEV_SMS_RETENTION_FUNCTION=scheduling-dev-<sms-retention-function-name> \
  backend/.venv/bin/python -m pytest -s backend/tests/test_dev_retention.py
```

It applies the same table, region, endpoint and CI guards as the race tests, and before any invoke it calls `GetFunctionConfiguration` and refuses unless the handler is exactly `scheduling.workers.note_retention.handler` or `scheduling.workers.sms_retention.handler` (matching the variable), `SCHEDULING_TABLE_NAME` is `scheduling-dev` and `BUSINESS_ID` is `dev-synthetic`. Nothing touches SQS or Twilio.

**Shared business.** Both workers purge the whole `BUSINESS_ID` partition, which is `dev-synthetic`, the owner's live test business. The test therefore never scrubs that partition. It seeds only under a run-specific client `synthetic-run-<id>`, SMS provider IDs `run-<id>-*`, and seven fictional `+1…555 01xx` phone numbers (all printed with `-s`), and its cleanup deletes only those keys, continuing past errors. Before seeding it reads every item in `BUSINESS#dev-synthetic` and **stops without seeding or invoking** if any existing record would be purged: an unheld note past its 12-month clock (using the client's last completed visit, as the worker does), an SMS body whose thread's last exchange is over 90 days old, or unheld consent/STOP evidence over four years old. If it stops, the owner's test data has aged out; hold or refresh those records first. Expiries in this precheck are evaluated one day ahead of the local clock, so clock skew cannot hide an item. The run keys (phones, client) are chosen only if no existing item uses them, else the run fails closed, and cleanup refuses to delete any key that existed before the run. After the run it asserts every pre-existing item is unchanged. **Do not use the dev owner app or text the dev number during a run.** The precheck is a point-in-time read: removing a legal hold from an owner note mid-run could get that note deleted, and the snapshot check would only report it afterwards.

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

## Rollback and monitoring

Keep the previous Lambda artifact/version for a code rollback. Disable schedules and event source mappings before rollback if workers cause errors. Roll back code separately from data; the table is retained and point-in-time recovery is the last-resort data recovery mechanism, not an automatic schema rollback. Before real data or scheduled workers are enabled, confirm the SNS subscription from the `AlarmEmail` mailbox, publish a synthetic test notification to the stack's `AlarmTopicArn`, and verify receipt. A subscription still pending confirmation is not a working destination. Check the `HoldOldestOverdueSeconds` and `OutboxOldestDueAgeSeconds` filters against synthetic worker logs and verify their 15-minute alarm transitions. The age values are the oldest due items **observed in each bounded worker page**, not a full-table maximum. Missing worker runs emit no age metric; the Lambda error alarms and schedule health need separate inspection. Inspect failed Lambda invocations, API errors, queue/DLQ depth, hold expiry age, outbox due age, and retention purge counts. Investigate a DLQ record before replay; the outbox record remains authoritative. Never replay an SMS blindly after an uncertain provider acceptance.

## Synthetic dev deployment checkpoint

This is the proposed **separate** authorization boundary, not an instruction to deploy now. The first change set is a `dev` stack in account `214965372605`, region `us-west-1`, using synthetic records and one owner test account. Show the CloudFormation change set, monthly cost estimate, intended alarm mailbox, and rollback steps to Enrique before executing it. The estimate, the exact resource list and parameters, and the rollback and cleanup plan are in [Synthetic dev stack: pre-authorization packet](DEV_STACK_PLAN.md). Use a role scoped to the named stack, its resources, and the CloudFormation execution role; a GitHub OIDC deploy role is not present in this repository, but [Dev deployment roles](#dev-deployment-roles) defines a scoped operator role and CloudFormation execution role for review. Do not use broad personal administrator credentials as a substitute for the scoped role. Do not put real credentials or customer records in parameters or change-set output.

1. Confirm account and region, the approved spend limit, and that `AlarmEmail` is an owner-monitored mailbox. Build and lint the exact commit. Use `Environment=dev`, `EnableSmsIngress=false`, `SmsSendEnabled=disabled`, an empty recipient allowlist, and inert Twilio and owner redirect placeholders. Keep every EventBridge schedule and the SQS sender mapping disabled (the three `*ScheduleState` parameters default to `DISABLED`; outbox dispatch and the sender mapping are hard-coded disabled with no parameter).
2. Create and inspect the change set; execute only after explicit provisioning authorization. Record stack ID, commit, parameter names (not secret values), resource ARNs, and the observed monthly cost baseline. Confirm the SNS email subscription, publish a synthetic notification, and verify receipt before using the alarms as a safety gate.
3. Complete the two-step owner callback/subject bootstrap above. Exercise authenticated calendar read/write with synthetic policy, clients, and appointments; reject a missing JWT and a different subject. Validate API asset routes and compare the rendered calendar with the synthetic records.
4. Run conditional transaction races against the dev table: two approvals for one slot, approval versus expiry, and replacement swap versus a stale competing write. Inspect the base records, audit entries, and outbox after each attempt; a losing transaction must leave no partial changes. Confirm the due GSIs and IAM grants work in the deployed environment.
5. Before enabling hold expiry, load synthetic due holds. Enable that schedule alone (its `*ScheduleState` parameter, through `scripts/dev/schedules.sh`), watch its report, age metric, and Lambda errors, and verify due work completes while stale index entries are harmless. Keep the outbox dispatch schedule **disabled**: the stack has no fake SQS consumer, so enabling it would accumulate messages. Test `DispatchService` against the dev table through an integration harness with a fake `IntentQueue` that records handoffs without touching SQS. The reviewed harness in `backend/tests/test_dev_outbox_queue.py` (see "Outbox SQS and DLQ proof") must pass against the deployed queue, consuming synthetic SQS messages without Twilio and exercising a controlled DLQ record, before outbox dispatch can be enabled; do not claim that gate passed until a deployed run is recorded. The deployed run is recorded in [DEV_STACK_PLAN.md section 5.2](DEV_STACK_PLAN.md#52-what-was-proven) (9 of 9 passed). Turn on note and SMS retention schedules only after synthetic expiration and legal-hold checks. Keep the live SMS sender and Twilio number disconnected.
6. If a gate fails, disable the affected schedule or event source, revert the Lambda artifact, and keep the retained table for inspection. Record the failure on #23 or the deployment issue. Tear down only after confirming how retained records, logs, and the Cognito test user will be handled; stack deletion alone does not remove retained data. Teardown is not the planned step after a passing checkpoint: on 2026-09-30 the owner decided to keep `dev` running as a pre-production test environment (see [DEV_STACK_PLAN.md section 5](DEV_STACK_PLAN.md#5-checkpoint-results-2026-09-30)).

## Deploy to dev with scripts

For frequent changes to the long-lived synthetic `dev` environment (#43), `scripts/dev/` wraps the manual procedures. They target only account `214965372605`, region `us-west-1`, stack `scheduling-dev`, and the Amplify app `scheduling-owner-dev`, and refuse anything else. Sign in first with `aws sso login --profile scheduling-dev-deployer`. Every script accepts `--dry-run`, which prints the commands without calling AWS (apart from the read-only `sts get-caller-identity` check).

| Script | What it does |
| --- | --- |
| `scripts/dev/deploy-backend.sh` | `sam build`, then `sam deploy --no-execute-changeset` as the deployer with the stack's own execution role. Every stack parameter keeps its live value (`UsePreviousValue`, so the alarm mailbox and `NoEcho` values are never read, retyped, or printed); only `PermissionsBoundaryArn` is passed, read from the live stack. It prints the change set (action, logical ID, type, replacement), warns about removals and replacements, lists names of any parameters that would change, and asks `y/N` before executing. `--yes` skips the prompt and is only for CI under #95's rules. With no changes it deletes the empty change set and exits 0. If any parameter that was not passed with `--param` would differ from the live stack, it refuses (names only are shown), even with `--yes`. An unexecuted change set is always deleted on exit. After executing, and also on the no-change path, it smoke-tests a real owner route (`/v1/owner/businesses/<BusinessId>/policy`): `401` without a token and a CORS preflight allowed from the app origin and not from a foreign origin. `--smoke-only` runs just that test. A parameter newly added to `template.yaml` must be supplied once with `--param Key=Value` (for a `*ScheduleState` parameter, the schedule's current live state). **Schedule guard (#97):** before the prompt it reads the target `State` of every added, modified or replaced EventBridge rule from the change set's processed template, compares the live state (`events:DescribeRule`) of the three parameterized rules (`HoldExpiryScheduleState`, `NoteRetentionScheduleState`, `SmsRetentionScheduleState`) with it, prints logical ID, live and target state, and refuses, even with `--yes`, if a state would change without the matching `--param`. Any other rule (outbox dispatch) must target `DISABLED`. Every run warns when a schedule's live state differs from its parameter value. |
| `scripts/dev/deploy-frontend.sh` | Reads `OwnerApiUrl` (without its trailing slash), `OwnerCognitoDomain` and `OwnerAppClientId` from the stack outputs, builds `frontend/` with `NEXT_PUBLIC_BUSINESS_ID=dev-synthetic` and `NEXT_PUBLIC_AUTH_MODE=cognito`, runs `check:export`, zips `frontend/out`, keeps the zip at `s3://<artifact bucket>/owner-app/<commit>.zip`, runs `create-deployment`, uploads, `start-deployment`, waits for `SUCCEED`, and checks that the app serves the new Next.js build ID and the CSP, HSTS and `X-Frame-Options` headers. It always runs `npm ci`. It refuses a dirty working tree unless `--allow-dirty`, which names the zip `<commit>-dirty.zip`. The Amplify app ID is derived from the live `OwnerAppOrigin` (or `--app-id`) and is never committed; the app's `defaultDomain` must reproduce `OwnerAppOrigin` exactly. It needs the optional Amplify grant above; until the owner applies it, run it with `--profile scheduling-dev-admin`. |
| `scripts/dev/status.sh` | Read-only: stack status, schedule and event source mapping states, the current app deployment, alarm states, and month-to-date budget spend (the budget line uses the admin profile if signed in, because the deployer cannot read budgets; `--no-budget` skips it). |
| `scripts/dev/schedules.sh enable\|disable <LogicalId>` | Sets one of three allowed schedules by running `deploy-backend.sh --param <X>ScheduleState=ENABLED\|DISABLED` (a full deploy of the current checkout with the usual change-set review: it prints the commit, refuses a dirty tree unless `--allow-dirty`, and never calls `enable-rule`; not an emergency tool, use the `disable-rule` procedure in DEV_STACK_PLAN.md section 3.1), refuses `OutboxDispatch*` and everything else, and prints the line to record on the issue. |

The role stack, the artifact bucket, the budget and the Cognito users stay manual. CI runs `shellcheck` on these scripts.

## Dev deployment roles

`infra/dev-deploy-roles.yaml` is the reviewable, least-privilege pair of roles the checkpoint above calls for. It is a plain CloudFormation template deployed as its **own small stack**, separate from `template.yaml`. Writing it created nothing: creating the role stack is an account change that needs Enrique's separate explicit authorization, like the checkpoint itself. Placeholders below (`<...>`) are supplied at run time and never committed.

**Account boundary.** The `dev` stack lives in a dedicated member account, so nothing else in the account belongs to another project and the account, not tags, isolates this project. Inside the account the roles still follow least privilege (names scoped to the stack, a Lambda permissions boundary, PassRole limits, escalation denies), which also protects the account's own administrator and deploy roles.

Two roles and six managed policies (one boundary, five attached to the roles), plus an optional seventh (`deploy-<StackName>-deployer-amplify`, created only when `AmplifyAppId` is set), all named `deploy-<StackName>-*` so they never match the `<StackName>-*` scope the execution role is allowed to manage:

| Role | Assumed by | Purpose |
| --- | --- | --- |
| `deploy-scheduling-dev-deployer` | The operator's Identity Center `AdministratorAccess` role in this account (or one explicit IAM principal, see below) | Create, review, execute and delete the dev stack's change sets and stack; run the post-deploy checkpoint actions |
| `deploy-scheduling-dev-cfn-exec` | `cloudformation.amazonaws.com` only, for stack `scheduling-dev` in this account | Create the `template.yaml` resources |
| `deploy-scheduling-dev-lambda-boundary` (managed policy) | Not assumable | Enforced ceiling for the Lambda roles the stack creates (see the tradeoff section) |

### Create the role stack

Run once, from an administrator session in account `214965372605` (Identity Center `AdministratorAccess`, signed in with MFA), in `us-west-1`. IAM is global, so create only one role stack per account for a given `StackName`. Do not run these commands until the owner authorizes creating the roles. The budget is created first ([section 1.6 of the packet](DEV_STACK_PLAN.md#16-approved-aws-budget-scoped-to-the-dedicated-account)), and the artifact bucket must already have its final name.

**Pre-creation check.** The administrator confirms that no IAM role named `scheduling-dev-*` and none named `deploy-scheduling-dev-*` already exists. The execution role is denied writes to any role without the boundary, so a pre-existing unbounded role would make the first deploy fail rather than be misused, but it should not be there. In a fresh dedicated account there should be none.

Create the change set, review it, then execute it, so the owner sees the final permissions before they exist:

```sh
aws cloudformation deploy --profile <admin profile> --stack-name scheduling-dev-roles --region us-west-1 \
  --template-file infra/dev-deploy-roles.yaml --capabilities CAPABILITY_NAMED_IAM \
  --no-execute-changeset \
  --parameter-overrides ArtifactBucketName=<artifact bucket>
aws cloudformation describe-change-set --profile <admin profile> --stack-name scheduling-dev-roles --region us-west-1 \
  --change-set-name <change set name printed by deploy>
# After the owner reviews the output:
aws cloudformation execute-change-set --profile <admin profile> --stack-name scheduling-dev-roles --region us-west-1 \
  --change-set-name <change set name>
```

The Identity Center defaults (`IdentityCenterPermissionSetName=AdministratorAccess`, `IdentityCenterRegion=us-west-1`, `RequireMfa=false`) are what the owner's setup needs, so no trust parameter is passed. Then deploy the dev stack with `PermissionsBoundaryArn=<LambdaRoleBoundaryArn output>` added to its parameters. After the dev stack exists, optionally tighten Cognito to that one pool by re-running the role-stack command with `OwnerUserPoolId=<pool id>` added.

### Who can assume the deployer role (trust design)

The operator does not sign in as an IAM user. They sign in through IAM Identity Center, which places them in a role named `AWSReservedSSO_AdministratorAccess_<random suffix>` at the path `/aws-reserved/sso.amazonaws.com/<region>/`. The suffix is random and changes if the permission set is ever removed and re-assigned, so the deployer role cannot name it in advance. Its trust policy therefore trusts the **account root** with a condition: `ArnLike` on `aws:PrincipalArn` matching `arn:aws:iam::<account>:role/aws-reserved/sso.amazonaws.com/<IdentityCenterRegion>/AWSReservedSSO_<IdentityCenterPermissionSetName>_*`. This is the pattern the AWS IAM Identity Center documentation gives for a role trusting a permission set (an `ArnLike` condition with a wildcard in place of the unique suffix), where the role ARN carries the Identity Center region unless the instance is in `us-east-1`. Trusting the root does not open the role to the whole account: the condition limits it to that one permission-set role, and the caller also needs `sts:AssumeRole` permission, which `AdministratorAccess` has.

- **MFA.** Identity Center sessions do not carry `aws:MultiFactorAuthPresent`, so requiring it would lock the operator out. `RequireMfa` therefore defaults to `false`, and a template rule rejects `RequireMfa=true` unless `TrustedPrincipalArn` is set, so that mistake fails at role-stack creation instead of creating an unassumable role. MFA is enforced at Identity Center sign-in, which the owner configured to be required on every sign-in. MFA is proved once per Identity Center session, so the session duration (default 8 hours) is the effective MFA window: the owner should shorten the permission-set and portal session duration in Identity Center settings, for example to 1 to 4 hours. The cached SSO token in `~/.aws/sso/cache` is sensitive while it is valid; do not copy or share it.
- **Name-prefix warning.** The `AWSReservedSSO_<PermissionSetName>_*` pattern also matches any permission set whose name begins with that name plus an underscore (for example `AdministratorAccess_x`). Do not create such permission sets in this account, or narrow `IdentityCenterPermissionSetName`.
- **Alternative operator.** Setting `TrustedPrincipalArn` to one IAM user or role ARN replaces the Identity Center pattern with that exact principal, and `RequireMfa=true` then requires an MFA-authenticated session, for an IAM-user operator with a virtual MFA device.
- **Not verified in an account.** The `aws:PrincipalArn` pattern is checked against the AWS documentation and `cfn-lint` only. The first authorized assume-role call confirms it; if it is denied, compare the role ARN in `aws sts get-caller-identity` (admin session) with the pattern.

### Assume the deployer role

The deployer role's ARN is in the role stack's `DeployerRoleArn` output. Keep the profiles in `~/.aws/config` and out of the repository.

1. Configure the administrator session once with `aws configure sso` (choose the account, the `AdministratorAccess` permission set, and region `us-west-1`) and sign in with `aws sso login --profile <admin profile>`. The browser sign-in asks for MFA. This profile is used only for one-time and administrator steps.
2. Add the deployer profile that assumes the role from the admin profile:

```ini
[profile scheduling-dev-deployer]
role_arn = <DeployerRoleArn>
source_profile = <admin profile>
region = us-west-1
```

No `mfa_serial` is set: MFA already happened at Identity Center sign-in. The temporary credentials of the assumed role last at most one hour (`MaxSessionDuration`), after which the CLI assumes it again from the still-valid SSO session.

AWS documents `source_profile` as naming another profile that supplies the credentials for `role_arn`. Using an Identity Center profile as that source is standard in AWS CLI v2 (the SDK's source-profile chain includes the SSO provider); the CLI page's wording about "long-term credentials" does not spell the SSO case out, and it was not run here because no AWS calls are allowed while writing this. Confirm with `aws sts get-caller-identity --profile scheduling-dev-deployer` on first use. As a backup only, if credentials do not resolve, make the source profile a `credential_process` profile that runs `aws configure export-credentials --profile <admin profile> --format process`. `sam deploy` uses the same profile chain, and needs `--profile scheduling-dev-deployer` explicitly.

Every change-set creation and stack deletion must pass the execution role, or the deployer role's own deny statement rejects it. The deployer role also denies resource-import change sets (`cloudformation:ImportResourceTypes`) as defense in depth; import is never needed here.

```sh
sam deploy --profile scheduling-dev-deployer --stack-name scheduling-dev --region us-west-1 \
  --role-arn <CloudFormationExecutionRoleArn> --s3-bucket <artifact bucket> --s3-prefix scheduling-dev \
  --capabilities CAPABILITY_IAM --no-execute-changeset ...
aws cloudformation delete-stack --profile scheduling-dev-deployer --stack-name scheduling-dev --role-arn <CloudFormationExecutionRoleArn> ...
```

### What each role can do

Deployer role, mapped to the checkpoint step that needs it (step numbers refer to the checkpoint list above; "teardown" and "rollback" numbers refer to sections 3.3 and 3.1 of `doc/DEV_STACK_PLAN.md`):

| Permission (scope) | Step |
| --- | --- |
| Create, describe, execute, delete change sets; describe stack, events and resources; get template; delete stack (only stack `scheduling-dev`; must name the execution role; plus the SAM transform) | 2, teardown 5 |
| `iam:PassRole` for the execution role only, only to `cloudformation.amazonaws.com` | 2 |
| Get/put/delete objects and versions, list, delete bucket (only the artifact bucket) | 2, teardown 10 |
| Cognito `AdminCreateUser`, `AdminGetUser`, `AdminSetUserPassword`, `AdminDeleteUser` (the stack's pool) | 3, teardown 4 |
| `sns:Publish`, get attributes, list subscriptions (topic `scheduling-alarms-dev`) | 2 |
| `events:EnableRule`, `DisableRule`, `DescribeRule` (rules `scheduling-dev-*`) | 5, rollback 1 |
| `amplify:GetApp`, `GetBranch`, `CreateDeployment`, `StartDeployment`, `GetJob` (the one app from `AmplifyAppId`, only when set) | Owner app deploys (`scripts/dev/deploy-frontend.sh`), rollback 5 |
| Lambda invoke, get function and configuration, put/delete reserved concurrency (functions `scheduling-dev-*`); get event source mapping; update event source mapping (to disable one; conditioned on the target function) | 5, rollback 1-2 |
| **Denied:** `lambda:UpdateFunctionConfiguration` and `lambda:UpdateFunctionCode`. Environment variables (SMS send switch, recipients, owner number) and code change only through a reviewed change set. Rollback brakes with reserved concurrency 0 and redeploys through the change set | none |
| DynamoDB item read/write and query/scan (table `scheduling-dev` and its indexes) | 4, 5 |
| DynamoDB describe, `UpdateContinuousBackups`, `DeleteTable` (that table) and `ListBackups` | teardown 2, 6 |
| Logs `FilterLogEvents`, `GetLogEvents`, describe streams, delete log group (`/aws/lambda/scheduling-dev-*`) | 4, 5, teardown 7 |
| CloudWatch describe alarms and history, get metric data/statistics, list metrics | 2, 5 |
| SQS `GetQueueAttributes`, `GetQueueUrl` (queues `scheduling-*-dev`) | 5, rollback 3 |
| SQS `SendMessage`, `ReceiveMessage`, `DeleteMessage` (queue `scheduling-outbox-dev` only) and `ReceiveMessage`, `DeleteMessage` (queue `scheduling-outbox-dlq-dev` only): the outbox queue proof. **Not granted:** purge, `SetQueueAttributes`, `ChangeMessageVisibility`, message-move tasks, the SMS conversation queues. Needs an owner-reviewed role-stack change set before first use | 5 |

Execution role:

- Lambda functions `scheduling-dev-*`, their event source mappings and permissions; HTTP APIs (API Gateway v2); EventBridge rules `scheduling-dev-*`; log groups `/aws/lambda/scheduling-dev-*` with retention and metric filters; CloudWatch alarms `scheduling-dev-*`; table `scheduling-dev` (create and update, **not** delete: it is `DeletionPolicy: Retain` and is removed by hand); queues `scheduling-*-dev`; topic `scheduling-alarms-dev` and its subscription; Cognito user pool, client and domain; read of the artifact prefix so CloudFormation can fetch the Lambda zips.
- IAM: create and manage only roles `scheduling-dev-*`; attach only `AWSLambdaBasicExecutionRole` and `AWSLambdaSQSQueueExecutionRole`; pass those roles only to `lambda.amazonaws.com`. Explicit denies cover attaching `AdministratorAccess*`, `PowerUserAccess` or `IAMFullAccess`, and any IAM action on `deploy-*` roles and policies.
- Read permissions were checked against the published CloudFormation handler permissions for each resource type the SAM output produces (create, read, update, delete, list), limited to the features `template.yaml` uses. Not granted because unused: KMS customer keys, VPC and EFS, table replicas and Kinesis streaming, custom Cognito domains and analytics, and Lambda code signing.
- `cloudformation:CreateChangeSet` on the AWS-owned SAM transform `Serverless-2016-10-31` only, not on any stack. When a change set names a service role, CloudFormation expands the transform as that role; without this grant, the first dev change set failed with `not authorized to perform: cloudformation:CreateChangeSet on resource: ...:aws:transform/Serverless-2016-10-31` (issue #43, 2026-09-30).
- Two further grants came from the first execution of the dev change set on 2026-09-30 (issue #43), which failed closed: `apigateway:TagResource` and `apigateway:UntagResource` on `/apis/*`, needed because the HTTP API stage handler tags the stage through these IAM actions; and read-only `lambda:GetEventSourceMapping` on `*`, needed because the rollback checked for a mapping whose creation had been interrupted before it had an ARN. After that failure, the execution role was compared with the published CloudFormation handler permissions (`aws cloudformation describe-type`) of all 17 resource types in the processed template. The only other missing permissions belong to features `template.yaml` does not set (customer KMS keys, VPC, table replicas and imports, resource policies, custom Cognito domains, code signing, log delivery) or to `list` handlers that stack operations do not call. They stay ungranted.
- **SSM is deliberately omitted.** `template.yaml` only writes the `/scheduling/dev/*` parameter paths into the *Lambda roles'* policies; CloudFormation never reads or creates a parameter. The `/scheduling/dev/*` `ssm:GetParameter` grant lives only in the boundary policy.

### Privilege-escalation tradeoff

A CloudFormation role that can create IAM roles is an escalation path unless constrained. This template layers **name-prefix scoping, an allowlist of two attachable AWS managed policies, explicit denies on admin policies, and a permissions boundary that is always enforced** (there is no switch to turn it off). `iam:PutRolePolicy` cannot be filtered by policy content, so without a boundary the execution role could write an arbitrary *inline* policy on a `scheduling-dev-*` role and pass it to a Lambda function it created. The boundary closes that hole: `iam:CreateRole`, `PutRolePermissionsBoundary`, `PutRolePolicy`, `DeleteRolePolicy`, `AttachRolePolicy` and `DetachRolePolicy` are denied unless the target role carries the boundary policy from the role stack (so a pre-existing unbounded `scheduling-dev-*` role cannot be written to), `iam:DeleteRolePermissionsBoundary` and `iam:UpdateAssumeRolePolicy` are always denied, and every IAM action on `deploy-*` roles and policies (including the boundary policy and all five attached policies: edit, new version, detach, delete) is denied. Whatever an inline policy grants, the effective permissions of the function roles cannot exceed the boundary: logs in the stack's `/aws/lambda/scheduling-dev-*` log groups, the dev table, the `scheduling-*-dev` queues, and `/scheduling/dev/*` SSM reads.

The boundary limits **permissions, not who can assume a role**. `CreateRole` accepts any trust policy and no IAM condition key exists for the trust document, so a deliberate template from the deployer could create a bounded role that trusts an external account, which could then use the boundary's permissions (read and write the dev table, read `/scheduling/dev/*` parameters). Editing a trust policy after creation is denied, but the creation-time gap remains and is closed only by change-set review: the owner checks every `AssumeRolePolicyDocument` in the change set for `lambda.amazonaws.com` only.

`template.yaml` has an optional `PermissionsBoundaryArn` parameter (default empty), applied through `Globals.Function.PermissionsBoundary` under a condition. Empty means unchanged behavior for local, CI and any unscoped deployment; **it must be set when deploying through the scoped execution role**, or role creation is denied. Order of creation: role stack first, then the dev stack with `PermissionsBoundaryArn=<LambdaRoleBoundaryArn output>` (also exported as `<role stack name>-LambdaRoleBoundaryArn`).
| Dev stack parameter | Value at the checkpoint |
| --- | --- |
| `PermissionsBoundaryArn` | The role stack's `LambdaRoleBoundaryArn` output (never a real ARN in the repository) |

### Known limits of the scoping

- **Account-wide scope for API Gateway and Cognito.** **HTTP APIs** (`apigateway:GET/POST/PUT/PATCH/DELETE` on `/apis/*`) and **Cognito user pools** (`userpool/*`) have random IDs, so the execution role can manage every API and every pool in the account and region, not just this stack's. `cognito-idp:CreateUserPool` has no resource-level scope at all. This is acceptable because the account is dedicated to this project: nothing else in it can be reached. If other workloads are ever placed in the account, this scope must be tightened first.
- **Event source mappings** have no resource type for create, and update and delete match on `*` too, so all three use `Resource: "*"` with the `lambda:FunctionArn` condition; get, tag and list-tags on a mapping cannot use that condition and are open to every mapping in the region. `lambda:ListEventSourceMappings` needs `*`.
- **`ListBackups`, `DescribeAlarms`, alarm history and metric reads, `DescribeLogGroups`, and `DescribeUserPoolDomain`** (and the Logs `DescribeResourcePolicies`) do not support narrower resources. They are read-only.
- The deployer's Cognito admin actions use the `aws:ResourceTag/aws:cloudformation:stack-name` condition until `OwnerUserPoolId` is set. That relies on CloudFormation tagging the pool with its stack name, which could not be verified because nothing may be created. If step 3 is denied, set `OwnerUserPoolId`. The confused-deputy `aws:SourceArn` condition on the execution role's trust policy likewise could not be exercised offline.
- Name-prefix scoping depends on CloudFormation's auto-naming (`<stack>-<LogicalId>-<random>`). Adding a resource with an explicit name in `template.yaml` (for example a queue outside `scheduling-*-dev`) needs a matching change to the execution role.
- A stack stuck in `UPDATE_ROLLBACK_FAILED` needs `ContinueUpdateRollback`, which is not granted; the administrator handles that case.
- **Not covered:** creating the artifact bucket, the Amplify app, and the AWS Budget. The bucket and the Amplify app are one-time steps done from the Identity Center administrator session under the existing authorization. The budget is created by the owner in the member account, signed in through Identity Center (see the packet's section 1.6). Budgets is used once, so a scoped role would add review cost for little safety. Amplify has an optional narrow add-on for repeat manual deployments (next bullet).
- **Optional Amplify deployment grant (issue #94).** The `AmplifyAppId` role-stack parameter is empty by default, and then the deployer has no Amplify access. After the owner creates the Amplify app, the owner updates the role stack through a reviewed change set with `AmplifyAppId=<app id>` (the segment between `main.` and `.amplifyapp.com` in the app origin; it is never committed). That attaches `deploy-scheduling-dev-deployer-amplify`, which allows only `amplify:GetApp`, `GetBranch`, `CreateDeployment`, `StartDeployment` and `GetJob` on that one app, its `main` branch, and that branch's jobs and deployments (Amplify checks `CreateDeployment` against `branches/main/deployments/*`, which the published Service Authorization Reference does not list; verified on dev, #94). It does not allow creating, updating or deleting apps or branches, `ListApps`, or any other app. Uploading the zip to `owner-app/` needs no new grant: the existing artifact-bucket object grant covers it. Amplify app deletion in teardown stays an administrator step.
- If CloudFormation rejects the execution role's trust conditions (`aws:SourceAccount` and `aws:SourceArn` are unverified with CloudFormation; the symptom is a change-set failure saying the role cannot be assumed), the fallback is for the owner to update the role stack with `TrustCloudFormationSourceArn=false`, which keeps only `aws:SourceAccount`, through a separately reviewed change. If `aws:SourceAccount` is also rejected, the trust policy has to be reviewed again.
- **A pre-existing unbounded `scheduling-dev-*` role can still be passed to a function.** `iam:PassRole` has no `iam:PermissionsBoundary` key, so the boundary deny cannot stop it (it does stop writes to such a role). Repeat the pre-creation role check (no `scheduling-dev-*` roles exist) before every deploy, and during change-set review confirm that every function's `Role` refers to a role created by the stack.
- **Verify on the first authorized run** whether `lambda:FunctionArn` is populated for `DeleteEventSourceMapping`. Create, update and delete are conditioned on it; if delete is denied, stack deletion fails on the two mappings (see the teardown note).
- Nothing here has been evaluated against a real account. The template passes `cfn-lint` offline; run IAM Access Analyzer policy validation and a change-set review on the first authorized run.

### Teardown order

1. Finish the dev stack teardown in `doc/DEV_STACK_PLAN.md` section 3.3 first: stack deleted (with `--role-arn`, step 5), retained table deleted (step 6), leftover log groups deleted (step 7), artifact bucket emptied and deleted (step 10). Run the deployer part of the nothing-billable-remains checklist (section 3.4) before the next step, because it needs the deployer role.
2. Delete the role stack **last**, as the administrator, not through the deployer role: `aws cloudformation delete-stack --profile <admin profile> --stack-name scheduling-dev-roles --region us-west-1`. The roles must outlive the dev stack because CloudFormation needs the execution role to delete the stack's resources; deleting the roles first strands the stack in `DELETE_FAILED`.
   If stack deletion fails on the event source mappings (an AccessDenied on `DeleteEventSourceMapping`), the fix is a reviewed change to the role stack (for example dropping the `lambda:FunctionArn` condition on delete), applied by the administrator before retrying. Do not work around it with broader credentials.
3. Remove the deployer profile from the AWS CLI config and confirm no role named `deploy-scheduling-dev-*` remains.
4. Optional, the owner's decision: close the member account for a complete cleanup (packet section 3.3, "Ultimate cleanup"). Closing removes the account's content only at permanent closure (up to 90 days later, and it can be reopened before then), including the budget, which lives in the member account.

## Expected running costs before deployment

The dated `us-west-1` estimate is in [Synthetic dev stack: pre-authorization packet](DEV_STACK_PLAN.md#1-cost-estimate-for-us-west-1). It prices the stack from the public AWS Price List (not a Pricing Calculator share link) for an idle stack, an active verification month, and a worst case with every schedule enabled all month. It replaces the earlier provisional planning envelope. The estimate is not an approved spending cap; the monthly budget and alert thresholds are Enrique's decision. The fixed monitoring footprint is eleven standard alarms with SMS ingress off and twelve with it on, plus two log-derived custom metrics once workers emit data. If all four schedules were enabled, hold expiry would run 8,640 times, outbox dispatch 43,200 times, and the two daily retention workers 60 times per month; the initial synthetic stack keeps them disabled. Twilio, OpenAI, taxes, and a custom domain are priced separately. Prices and account-level free tiers change, so refresh the estimate before authorizing.
