# Synthetic `dev` stack: pre-authorization packet

Prepared 2026-09-29 for issue [#43](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/43). This document is **review material only**. It does not authorize, and nothing in it performed, any deployment, AWS account access, spending, owner account creation, or live SMS. The commands are documentation, not instructions to run now. The checkpoint itself is described in [Pilot infrastructure plan](PILOT_INFRASTRUCTURE.md#synthetic-dev-deployment-checkpoint).

Target if authorized: the **dedicated dev member account `214965372605`** (`scheduling-dev`), region `us-west-1`, stack name `scheduling-dev`, `Environment=dev`, synthetic data only. The account belongs to the owner's AWS Organization; account `339713090487` is the organization's management account and is used for billing and organization administration only, not for this stack. The operator signs in through IAM Identity Center (MFA at every sign-in). See [Dev deployment roles](PILOT_INFRASTRUCTURE.md#dev-deployment-roles).

## Summary for Enrique

**Nothing is authorized yet.** This document only lays out what a "yes" would mean. No deployment, spending, owner account creation, or live text has happened or will happen without your separate approval.

- **What it would cost per month** (us-west-1, list price, 12-month free tier not assumed): about **$1.14** idle, **$2.43** in an active test month, and **$3.20** in a worst case where every scheduled job ran all month. After the allowances AWS always gives away for free, those become about $0.11, $0.69, and $0.75. Twilio, OpenAI, and taxes are not included.
- **Approved budget (2026-09-30):** a **$10 per month** AWS budget, with email alerts when actual spend reaches $5, $8, and $10 and when AWS forecasts $10. It is scoped to the dedicated account and is created before the stack ([section 1.6](#16-approved-aws-budget-scoped-to-the-dedicated-account)). A budget warns; it does not stop spending.
- **What saying yes to the dev checkpoint would authorize:** (1) creating a private, versioned, encrypted S3 bucket for build artifacts and uploading the build; (2) creating a *change set* (below); (3) executing it, which creates the 61 resources listed in section 2, all with SMS off and every schedule off; (4) creating one owner test user and one Amplify app (the budget is created earlier, first); (5) the checkpoint tests on synthetic data, including switching on the hold-expiry schedule and later the two retention schedules; and (6) the teardown in section 3, which stays documented and available but is no longer the planned next step after the checkpoint (owner decision 2026-09-30, [section 5](#5-checkpoint-results-2026-09-30)).
- **What it would not authorize:** SMS ingress or sending, Twilio, OpenAI, outbox dispatch, any real client data, the `pilot` stack, or rollback redeploys unless you approve those in decision 4.

Two terms, once. A **change set** is CloudFormation's preview of what a deployment would create; nothing exists until it is executed, but creating it still needs AWS credentials, uploads the build to S3, and leaves an empty stack in `REVIEW_IN_PROGRESS`. **PITR** (point-in-time recovery) is DynamoDB's continuous backup that can restore the table to any second in the last 35 days.

## Decisions Enrique must make (open, not decided here)

Status, 2026-09-30 (issue #43): decisions 1 to 4 and 7 were answered by the owner. The owner then chose a **dedicated member account** for `dev` (replacing the shared account `339713090487`, which held other proof-of-concept projects) and signs in through IAM Identity Center with MFA at every sign-in. Decision 5 was answered: schedules were first enabled out of band with `aws events enable-rule`, recorded on #43, and are now template parameters (#97; see [section 2.5](#25-enabling-schedules-template-parameters-decided-issue-97) and [section 5.6](#56-current-state)). Decision 6 is answered by `infra/dev-deploy-roles.yaml` in this repository, which still needs the owner's separate authorization to create. The provisioning, budget, rollback and teardown answers carry over to the new account. Decision 7 is superseded by the owner's 2026-09-30 decision to keep `dev` running ([section 5.1](#51-owner-decision-2026-09-30)).

1. **Monthly budget amount and alert thresholds.** Approved: $10 per month, see [section 1.6](#16-approved-aws-budget-scoped-to-the-dedicated-account).
2. **The owner-monitored alarm mailbox** (`AlarmEmail`). No address is recorded in the repository.
3. **Provisioning authorization.** It covers, each a separate billable or account-changing step: creating the artifact bucket (private, versioned, encrypted) and uploading the build; **creating the change set** (uses credentials, uploads billable artifacts, and leaves the stack in `REVIEW_IN_PROGRESS`); executing the change set; creating the owner test user, the Amplify app, and the budget.
4. **Whether rollback redeploys are pre-authorized** as part of this checkpoint, or each one needs a fresh yes (see [section 3.1](#31-stop-on-a-failed-safety-gate)).
5. **How schedules get enabled** (see [section 2.5](#25-enabling-schedules-template-parameters-decided-issue-97)). Answered first as out-of-band `aws events enable-rule`, recorded on #43; replaced by per-schedule template parameters in #97.
6. **The scoped deployment role.** Answered by `infra/dev-deploy-roles.yaml`, the deployer and CloudFormation execution roles for the dedicated account. Creating them still needs the owner's separate authorization; the checkpoint forbids broad administrator credentials for the deployment itself.
7. **Whether the synthetic dev table is deleted at teardown or kept for inspection.** Answered 2026-09-30: delete the retained synthetic dev table after results are recorded (see [section 3.3](#33-full-teardown-in-order)). Superseded 2026-09-30: `dev` is kept running; the table is deleted only if teardown is later chosen ([section 5.1](#51-owner-decision-2026-09-30)).

## 1. Cost estimate for `us-west-1`

### 1.1 Method and provenance

- **Not an AWS Pricing Calculator share link.** The issue asks for a Pricing Calculator estimate. The calculator UI cannot be driven from this environment, so this estimate is built from the public AWS Price List bulk API, fetched anonymously on **2026-09-29**: `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<ServiceCode>/current/us-west-1/index.json` (AWS Budgets has no regional file; `.../AWSBudgets/current/index.json`). The file publication dates ranged from 2026-09-11 to 2026-09-28. [Section 1.7](#17-reproducing-this-in-the-pricing-calculator) lists the inputs to reproduce it in the calculator.
- All Lambda functions are `arm64`, 256 MB (`Globals.Function` in `template.yaml`), Python 3.12. All SQS queues use SSE-SQS (`SqsManagedSseEnabled: true`), which has no separate charge and no KMS request fees. The DynamoDB table sets `SSEEnabled: true` without a key, which CloudFormation treats as encryption with the AWS managed key `aws/dynamodb`. There is no monthly key fee for it; KMS request fees can apply and are included as a small assumption below.
- The Cognito user pool sets no `UserPoolTier`. The template selects nothing, so the account default applies. As far as the CloudFormation documentation says, that default is **Essentials**. Confirm with `describe-user-pool` after any deployment. Lite would be $0.0055/MAU instead of $0.015/MAU; the difference is immaterial for one user.
- A month is 30 days (720 hours), matching the invocation counts in the pilot plan.
- Excluded: Twilio, OpenAI, taxes, a custom domain, support plans, and anything else in the account.

### 1.2 Unit prices used (fetched 2026-09-29)

| Service | Item | Unit price | Price List SKU / usage type |
| --- | --- | --- | --- |
| Lambda | Requests, arm64 | $0.20 per million | `EQE7YY7WBWZFB46G` / `USW1-Request-ARM` |
| Lambda | Duration, arm64, first 7.5 billion GB-s | $0.0000133334 per GB-s | `XVFAA23C65JJZNNW` / `USW1-Lambda-GB-Second-ARM` |
| API Gateway | HTTP API, first 300 million requests | $1.17 per million | `C3CJ8ENJSX6PD83A` / `USW1-ApiGatewayHttpRequest` |
| DynamoDB | On-demand read request units | $0.1395 per million | `2B5A2GVFNUDVUX8R` / `USW1-ReadRequestUnits` |
| DynamoDB | On-demand write request units | $0.695 per million | `ZYXUKUJEQQX357EY` / `USW1-WriteRequestUnits` |
| DynamoDB | Table and GSI storage beyond 25 GB | $0.28 per GB-month | `MQQYCYRA3HGBZJBA` / `USW1-TimedStorage-ByteHrs` |
| DynamoDB | Point-in-time recovery storage | $0.224 per GB-month | `DZKF38259JUU2BJ8` / `USW1-TimedPITRStorage-ByteHrs` |
| SQS | Standard requests, tier 1 | $0.40 per million | `D8K2W3D7HJHVWMFG` / `USW1-Requests-Tier1` |
| SNS | Email notifications after the first 1,000 | $2.00 per 100,000 | `JU7YA4BUTNTXF9CF` / `USW1-DeliveryAttempts-SMTP` |
| CloudWatch | Custom log data ingested (Standard class) | $0.67 per GB | `ZQZVEJJY7MRQHAQV` / `USW1-DataProcessing-Bytes` |
| CloudWatch | Log storage | $0.033 per GB-month | `4VRR4NRNWJWSSBV8` / `USW1-TimedStorage-ByteHrs` |
| CloudWatch | Standard-resolution alarm | $0.10 per alarm-month | `8JFRJZU4SGUUHYGZ` / `USW1-CW:AlarmMonitorUsage` |
| CloudWatch | Custom metric (first 10,000; includes metric-filter metrics) | $0.30 per metric-month | `P8WWN4FPGA3H5CMY` / `USW1-CW:MetricMonitorUsage` |
| Cognito | Essentials MAU | $0.015 per MAU | `5J74Y3YBXDR26WXU` / `USW1-CognitoEssentialsMAU` |
| EventBridge | Scheduled rules (SAM `Schedule` events create EventBridge rules, not EventBridge Scheduler schedules): no charge for the rule or its target invocations. The Scheduler SKU (`XAWTXSQRCY6JTPGR` / `USW1-ScheduledInvocation`) and its 14 million free invocations do not apply. | $0 | Lambda invocations they trigger bill as Lambda |
| SSM Parameter Store | Standard parameter (incl. SecureString): no per-parameter charge; advanced tier $0.05 per parameter-month | $0 (none created) | `S4ET88YAD97RYGDK` / `USW1-PS-Advanced-Param-Tier1` (advanced only) |
| KMS | Requests (customer-managed key: $1 per key-month, not used) | $0.03 per 10,000 | `H4VQ25AJP5STBY2V` / `us-west-1-KMS-Requests`; key `NUYE54EYMNVW49QE` |
| Amplify Hosting | Build minutes (standard build instance) | $0.01 per minute | `HW5PZSXXESDDX6HZ` / `USW1-BuildDuration` |
| Amplify Hosting | Stored artifacts | $0.023 per GB-month | `2TS8VR224VE3ZZVU` / `USW1-DataStorage` |
| Amplify Hosting | Data served | $0.15 per GB | `BPEY374TMPREZMYH` / `USW1-DataTransferOut` |
| AWS Budgets | Cost budget without actions | $0 | `T2UVJ9XYT52FBFAF` / `BudgetsUsage`. Action-enabled budgets are free for 62 budget-days, then $0.10/day (`H8ZKA3NDUDG5HWHM`). Not used. |
| Data transfer | Out to the internet, first 10 TB | $0.09 per GB | `AHRNA43Y8M88C9BD` / `USW1-DataTransfer-Out-Bytes` |
| S3 (SAM artifacts) | Standard storage | $0.026 per GB-month | `TZP3WS2ZP5WNPJT2` / `USW1-TimedStorage-ByteHrs`; PUT $0.0055 per 1,000 (`CE79BJ8EJA6B9EDP`) |

CloudFormation charges nothing for native AWS resource types. IAM, the SNS topic policy, Lambda permissions, and log-group creation are free. Nothing in the stack needs a NAT gateway, VPC, X-Ray, WAF, or a provisioned-concurrency setting.

### 1.3 Free allowances: always-free versus 12-month

Two totals are shown for each scenario. **List** applies no allowance at all. **After always-free** subtracts only the allowances that AWS states are not time-limited. **No total assumes the 12-month free tier**, because this account's eligibility is unknown.

| Allowance | Kind | Source checked 2026-09-29 |
| --- | --- | --- |
| Lambda: 1 million requests and 400,000 GB-s per month | Always free | Lambda pricing page |
| SQS: 1 million requests per month | Always free | SQS pricing page |
| SNS: 1,000 email notifications and 1 million API requests per month | Always free | Price List (`JU7YA4BUTNTXF9CF`, `7M9WX9J3V4D36FUV`) |
| CloudWatch: 10 alarm metrics, 10 custom metrics, 5 GB log ingestion per month | Always free | CloudWatch pricing page |
| Cognito user pools: 10,000 MAU (direct sign-in) | Always free | Cognito pricing page |
| DynamoDB: 25 GB storage | Always free | Price List (`MQQYCYRA3HGBZJBA`) |
| KMS: 20,000 requests per month | Always free | Price List (`VTFSAGP364M6QE4A`) |
| Data transfer out: 100 GB per month aggregate | Always free | Data transfer pricing (Price List text says "beyond the global free tier") |
| API Gateway HTTP API 1 million requests, Amplify build/storage/serving | **12-month free tier only** | Not applied |
| DynamoDB requests, PITR, and CloudWatch alarms beyond 10 | No allowance | Billed |

The pricing pages were fetched as HTML for the allowance rows; those pages are not versioned like the Price List, so re-check them at authorization time.

### 1.4 Scenarios and assumptions

**(a) Idle baseline.** Stack deployed; every EventBridge rule disabled; both SQS mappings disabled; `EnableSmsIngress=false`; Amplify app created with one manual deployment, no builds. Traffic: 100 owner API requests (300 ms each), 1,000 read and 200 write request units, 0 monthly active users, 1 MB of logs. No worker emits data, so the two log-derived custom metrics do not exist. The 11 alarms still bill.

**(b) Active verification month.** The checkpoint steps 1 to 6 in the pilot plan, with generous headroom. Synthetic dev table of 5 MB (0.005 GB) with PITR on, and 5 synthetic clients with notes.
- 5,000 owner API requests (the pilot-plan scenario), 300 ms average, 256 MB.
- 200,000 read and 100,000 write request units in total, including the note queries below. This covers the API calls, three transaction-race families run repeatedly (two approvals for one slot, approval versus expiry, replacement swap versus stale write), and the fake-queue `DispatchService` harness. Transactions cost twice the units of ordinary writes, and each write to an indexed item also writes the GSIs.
- Hold expiry enabled for 7 days at `rate(5 minutes)`: 2,016 runs at 1 s. Note and SMS retention enabled for 7 days after the expiry and legal-hold checks: 14 runs at 5 s (7 of them note retention). Outbox dispatch stays **disabled**, as in the pilot plan. 1,000 extra manual or harness invocations at 1 s.
- **Last-visit queries:** 5 clients with notes x 7 note-retention runs, plus 300 owner note-list or note-create calls = 335 strongly consistent, client-keyed queries. Each reads at most one small visit item, independent of table size; this is covered by the 200,000 read-unit allowance above.
- 10,000 SQS requests (300 synthetic intents through a fake consumer, plus the DLQ exercise; the stack has no consumer and the sender mapping stays disabled).
- 0.1 GB of logs ingested. Both metric filters emit at least once (the alarm-transition checks), so 2 custom metrics bill.
- 1 owner monthly active user. 10 Amplify builds of 4 minutes (manual deploy uses no build minutes; the figure is kept unchanged), 0.05 GB stored, 0.1 GB served. 0.1 GB of API responses out. 10,000 KMS requests for the DynamoDB managed key (an assumption). 0.25 GB in the SAM artifact bucket (several build versions of a Lambda zip, which is a small package: `backend/` is under 1 MB before dependencies).

**(c) Worst case: all four schedules run all month.** As (b), but 30 days of every schedule, using the invocation counts in the pilot plan: hold expiry 8,640, outbox dispatch 43,200, note retention 30 and SMS retention 30. Synthetic dev table of 5 MB with 5 synthetic clients with notes, and 1 GB of logs (deliberate headroom).
- Runs: hold expiry 1 s, outbox dispatch 0.5 s, retention 5 s.
- Each hold-expiry run reads 1 read unit, each outbox run 0.5.
- **Last-visit queries.** 5 clients x 30 note-retention runs + 300 note-list or note-create calls = 450 client-keyed queries, covered by the read-unit allowance above. SMS retention uses queries and is assumed to cost about 100 read units per run. Confirming or changing a visit also writes its small client-visit item transactionally.
- `last_visit_end` uses a strongly consistent reverse query on a client-specific base-table partition with `Limit=1`. Its read cost depends on one visit item, rather than total table size. The client-visit item is created or removed atomically with appointment changes.
- The outbox queue mapping stays disabled, so messages produced by outbox dispatch would accumulate. Enabling either SQS event source mapping adds continuous long-poll receive requests billed as SQS requests. That is not included. A rough upper bound is about $0.26 per queue-month (about five pollers polling every 20 seconds at the $0.40/million price), which is an estimate to be measured, not a quote.

### 1.5 Monthly totals

| Line | (a) Idle | (b) Active verification | (c) All schedules all month |
| --- | ---: | ---: | ---: |
| Lambda (requests and duration) | $0.00 | $0.02 | $0.12 |
| API Gateway HTTP API | $0.00 | $0.01 | $0.01 |
| DynamoDB request units | $0.00 | $0.10 | $0.10 |
| DynamoDB storage | $0.00 | $0.00 | $0.00 |
| DynamoDB PITR | $0.00 | $0.00 | $0.00 |
| SQS | $0.00 | $0.00 | $0.00 |
| SNS email | $0.00 | $0.00 | $0.00 |
| CloudWatch alarms (11) | $1.10 | $1.10 | $1.10 |
| CloudWatch custom metrics | $0.00 | $0.60 | $0.60 |
| CloudWatch logs (ingest and storage) | $0.00 | $0.07 | $0.70 |
| Cognito | $0.00 | $0.02 | $0.02 |
| EventBridge scheduled rules | $0.00 | $0.00 | $0.00 |
| SSM Parameter Store | $0.00 | $0.00 | $0.00 |
| KMS requests | $0.03 | $0.03 | $0.03 |
| Amplify Hosting | $0.00 | $0.42 | $0.42 |
| AWS Budgets | $0.00 | $0.00 | $0.00 |
| Data transfer out | $0.00 | $0.01 | $0.01 |
| S3 SAM artifacts | $0.01 | $0.01 | $0.01 |
| **Total at list price (no allowances)** | **$1.14** | **$2.37** | **$3.12** |
| **Total after always-free allowances** | **$0.11** | **$0.69** | **$0.75** |

Rows are rounded to the cent, so they may not add exactly to the totals, which are computed from unrounded values (for example, (a) also includes about $0.001 of Amplify storage and a few hundredths of a cent of requests).

Reading the table:
- The dominant fixed cost is the **11 CloudWatch alarms** ($1.10 at list; $0.10 after the 10-alarm allowance). Enabling SMS ingress adds a 12th ($0.10).
- The idle stack costs about $1 per month at list price. It is not zero because alarms, the DynamoDB PITR/storage, and Amplify storage bill while idle.
- Log ingestion is $0.67 per GB in `us-west-1`, which is more than the widely quoted `us-east-1` price. Log volume growth is the most likely source of surprise; every extra GB of ingested logs adds $0.67 before the 5 GB allowance.
- Last-visit lookup cost is independent of table size. At 20 clients with notes and 30 daily runs, retention makes about 600 small keyed queries; owner note calls add one each. A 10 GB table also adds about $2.2 a month of PITR before the 25 GB storage allowance.
- This estimate sits well below the earlier provisional $10 to $30 planning envelope, which the pilot plan no longer carries.

### 1.6 Approved AWS Budget, scoped to the dedicated account

**Approved by the owner on 2026-09-30; the amounts and thresholds are unchanged.**

- **Monthly budget: $10.00.** It is about three times the worst-case dev list-price total ($3.12), which leaves headroom for log growth or a mistaken schedule, and the first alert ($5) already sits above that worst case, so any alert signals something unexpected. **This is a dev-only figure.** The `pilot` stack needs its own budget in its own account before onboarding real records.
- **Type:** monthly cost budget with no budget actions (actions cost $0.10 per budget-day after 62 days and would need extra IAM).
- **Alerts, sent to the owner-monitored mailbox:**
  - Actual spend at 50 percent ($5.00), 80 percent ($8.00), and 100 percent ($10.00).
  - Forecasted spend at 100 percent ($10.00).
- **Placement: the dedicated member account `214965372605`, scope all services (the whole account).** This is an accepted deviation from the earlier plan, which placed the budget in the management account `339713090487` with a linked-account filter on `214965372605`. A brand-new member account does not appear in the management account's linked-account filter until it has billing data, so the budget was created in the member account instead. The budget is named `scheduling-dev`, a monthly cost budget of $10.00 USD; the amounts and alerts are unchanged. Every cost in the account belongs to this project, so no tag is involved and there is nothing to activate.
- **Who creates it:** the owner, signed in to the member account through IAM Identity Center (AdministratorAccess), as the **first** provisioning step ([section 2.3](#23-stack-parameters), order of creation). It was created this way on 2026-09-30 (issue #43). Neither the deployer role nor the execution role can create budgets, and agents do not hold these credentials. It belongs to the account-changing steps already authorized under decision 3.
- **Schedules stay off until it exists.** No schedule or event source mapping is enabled before the budget exists. That is satisfied by construction because the budget is created before the stack.
- **Limits.** A budget alerts; it does not stop spending. Cost data lags by hours, so an alert can arrive after money is spent. The budget lives in the member account, so it is removed with the account's content at permanent closure. There is no management-account budget to delete at teardown; before closing the account, delete it with `delete-budget` in the member account (section 3.3, step 9).

### 1.7 Reproducing this in the Pricing Calculator

Set the region to US West (N. California) and add one estimate group with these inputs (`arm64`, all standard tiers, monthly, no free tier assumed):

- **Lambda:** architecture Arm, 256 MB, ephemeral storage 512 MB, requests and average duration per the scenario (for example (b): about 8,000 requests, 1,150 GB-s).
- **API Gateway:** HTTP API, 5,000 requests, average 2 KB.
- **DynamoDB:** on-demand, standard table class, data storage as in the scenario, PITR on with the same data size, read and write request units as in the scenario (the calculator asks for item size and item counts; use 1 KB items and match the unit totals), no global tables, no streams.
- **SQS:** standard queue, 10,000 requests, SSE-SQS.
- **SNS:** email, 20 notifications.
- **CloudWatch:** 11 standard alarms, 2 custom metrics, 0.1 GB (b) or 1 GB (c) of log ingestion, 30-day retention.
- **Cognito:** Essentials tier, 1 MAU, direct sign-in.
- **EventBridge:** scheduled rules, no charge (the Lambda invocations they trigger are in the Lambda input).
- **SSM Parameter Store:** none.
- **KMS:** AWS managed key, 10,000 requests (calculator shows zero key cost).
- **Amplify Hosting:** 40 build minutes (unchanged; manual deploy uses none), 0.05 GB stored, 0.1 GB served.
- **AWS Budgets:** 1 cost budget.
- **S3:** 0.25 GB standard.
- **Data transfer:** 0.1 GB out.

Toggle the always-free allowances off to compare with the "list price" row. The calculator applies its own free-tier options; do not enable "12-month free tier" for a comparison with this packet.

## 2. Resource list and stack parameters

### 2.1 Resources created with the dev settings

Settings: `Environment=dev`, `EnableSmsIngress=false`, `SmsSendEnabled=disabled`, `EnableSmsConversations=disabled`, every other parameter at its safe default. The resource list was produced by running the pinned SAM translator (`aws-sam-translator` 1.113.0, installed in a scratch virtual environment) offline against `template.yaml`, with `CodeUri` replaced by a placeholder S3 URI. `sam validate` and `sam build` were **not** run because `sam validate` may look for AWS credentials. Nothing contacted AWS. Re-run `sam validate --lint` and `sam build` on the exact commit at authorization time.

The translator produces **61 resources** in this configuration. "Deletion" is the CloudFormation deletion policy; only the table sets one, so the rest are deleted with the stack.

| Logical ID | Type | Purpose | Deletion |
| --- | --- | --- | --- |
| `SchedulingTable` | `AWS::DynamoDB::Table` | On-demand table `scheduling-dev` with `PK`/`SK`, `HoldDueIndex`, `OutboxDueIndex` (both `KEYS_ONLY`), PITR on, SSE on, TTL on `expires_at_epoch` | **Retain** (also `UpdateReplacePolicy: Retain`) |
| `OutboxQueue` | `AWS::SQS::Queue` | `scheduling-outbox-dev`, visibility 180 s, 4-day retention, SSE-SQS, redrive to DLQ after 5 receives | Delete |
| `OutboxDeadLetterQueue` | `AWS::SQS::Queue` | `scheduling-outbox-dlq-dev`, 14-day retention, SSE-SQS | Delete |
| `SmsConversationQueue` | `AWS::SQS::Queue` | `scheduling-sms-conversation-dev`, same settings, redrive after 5 receives | Delete |
| `SmsConversationDeadLetterQueue` | `AWS::SQS::Queue` | `scheduling-sms-conversation-dlq-dev`, 14-day retention | Delete |
| `AlarmTopic` | `AWS::SNS::Topic` | `scheduling-alarms-dev` with an email subscription to `AlarmEmail` (pending until confirmed) | Delete |
| `AlarmTopicPolicy` | `AWS::SNS::TopicPolicy` | Lets CloudWatch alarms in this account publish to the topic | Delete |
| `OwnerUserPool` | `AWS::Cognito::UserPool` | `scheduling-owner-dev`, email username, admin-create-only, MFA off, 14-character passwords | Delete |
| `OwnerUserPoolDomain` | `AWS::Cognito::UserPoolDomain` | Hosted-UI domain prefix from `CognitoDomainPrefix` | Delete |
| `OwnerUserPoolClient` | `AWS::Cognito::UserPoolClient` | Public client, authorization-code flow, `openid` scope, callback and logout `${OwnerAppOrigin}/` | Delete |
| `OwnerHttpApi` | `AWS::ApiGatewayV2::Api` | Owner HTTP API. Routes, Lambda integrations, the Cognito JWT authorizer, and CORS are inline in the API body. Only `GET`, `POST`, `PUT`, `PATCH`, `DELETE` on `/v1/owner/{proxy+}` | Delete |
| `OwnerHttpApiApiGatewayDefaultStage` | `AWS::ApiGatewayV2::Stage` | The `$default` stage, auto-deploy, no access logging | Delete |
| `OwnerApiFunction` | `AWS::Lambda::Function` | `scheduling.owner_lambda.handler`, 30 s | Delete |
| `OwnerApiFunctionRole` | `AWS::IAM::Role` | Basic execution plus table item and index access | Delete |
| `OwnerApiFunctionOwnerProxyGetPermission`, `...PostPermission`, `...PutPermission`, `...PatchPermission`, `...DeletePermission` (5) | `AWS::Lambda::Permission` | Lets the owner API invoke the function, one per method | Delete |
| `OwnerApiLogGroup` | `AWS::Logs::LogGroup` | 30-day retention | Delete |
| `SmsConversationFunction` | `AWS::Lambda::Function` | Conversation worker, `scheduling.workers.sms_conversation.handler`; inert unless `SMS_CONVERSATION_ENABLED=authorized` | Delete |
| `SmsConversationFunctionRole` | `AWS::IAM::Role` | Basic and SQS execution managed policies, table access, read of the `openai/api-key` SSM parameter, receive from its queue | Delete |
| `SmsConversationFunctionReceipts` | `AWS::Lambda::EventSourceMapping` | SQS mapping, batch 10, **Enabled=false** with these settings | Delete |
| `SmsConversationLogGroup` | `AWS::Logs::LogGroup` | 30-day retention | Delete |
| `SmsSenderFunction` | `AWS::Lambda::Function` | Outbox SMS sender, `scheduling.workers.sms_outbox.handler`, `SMS_SEND_ENABLED=disabled` | Delete |
| `SmsSenderFunctionRole` | `AWS::IAM::Role` | Basic and SQS execution managed policies, table access, read of the Twilio SSM parameter, receive from the outbox queue | Delete |
| `SmsSenderFunctionOutbox` | `AWS::Lambda::EventSourceMapping` | SQS mapping on `OutboxQueue`, **Enabled=false** | Delete |
| `SmsSenderLogGroup` | `AWS::Logs::LogGroup` | 30-day retention | Delete |
| `HoldExpiryFunction` | `AWS::Lambda::Function` | `scheduling.workers.expiry.expire_due_handler` | Delete |
| `HoldExpiryFunctionRole` | `AWS::IAM::Role` | Basic execution plus table and index access | Delete |
| `HoldExpiryFunctionSweep` | `AWS::Events::Rule` | `rate(5 minutes)`, **State=DISABLED** | Delete |
| `HoldExpiryFunctionSweepPermission` | `AWS::Lambda::Permission` | EventBridge may invoke the function | Delete |
| `HoldExpiryLogGroup` | `AWS::Logs::LogGroup` | 30-day retention; source of the hold-age metric filter | Delete |
| `OutboxDispatchFunction` | `AWS::Lambda::Function` | `scheduling.workers.outbox.dispatch_due_handler` | Delete |
| `OutboxDispatchFunctionRole` | `AWS::IAM::Role` | Basic execution, `OutboxDueIndex` query, item get/update, send to the outbox queue | Delete |
| `OutboxDispatchFunctionSweep` | `AWS::Events::Rule` | `rate(1 minute)`, **State=DISABLED** | Delete |
| `OutboxDispatchFunctionSweepPermission` | `AWS::Lambda::Permission` | EventBridge may invoke the function | Delete |
| `OutboxDispatchLogGroup` | `AWS::Logs::LogGroup` | 30-day retention; source of the outbox-age metric filter | Delete |
| `NoteRetentionFunction` | `AWS::Lambda::Function` | `scheduling.workers.note_retention.handler`, 60 s | Delete |
| `NoteRetentionFunctionRole` | `AWS::IAM::Role` | Basic execution plus table access including `Query` | Delete |
| `NoteRetentionFunctionDaily` | `AWS::Events::Rule` | `cron(0 9 * * ? *)`, **State=DISABLED** | Delete |
| `NoteRetentionFunctionDailyPermission` | `AWS::Lambda::Permission` | EventBridge may invoke the function | Delete |
| `NoteRetentionLogGroup` | `AWS::Logs::LogGroup` | 30-day retention | Delete |
| `SmsRetentionFunction` | `AWS::Lambda::Function` | `scheduling.workers.sms_retention.handler`, 60 s | Delete |
| `SmsRetentionFunctionRole` | `AWS::IAM::Role` | Basic execution plus table access including `Scan` | Delete |
| `SmsRetentionFunctionDaily` | `AWS::Events::Rule` | `cron(0 10 * * ? *)`, **State=DISABLED** | Delete |
| `SmsRetentionFunctionDailyPermission` | `AWS::Lambda::Permission` | EventBridge may invoke the function | Delete |
| `SmsRetentionLogGroup` | `AWS::Logs::LogGroup` | 30-day retention | Delete |
| `HoldOverdueAgeMetric`, `OutboxDueAgeMetric` (2) | `AWS::Logs::MetricFilter` | Publish `HoldOldestOverdueSeconds` and `OutboxOldestDueAgeSeconds` into namespace `Scheduling/dev` from worker logs | Delete |
| `HoldOverdueAgeAlarm`, `OutboxDueAgeAlarm` | `AWS::CloudWatch::Alarm` | Oldest due item over 15 minutes | Delete |
| `OutboxDlqAlarm`, `SmsConversationDlqAlarm` | `AWS::CloudWatch::Alarm` | Any visible message in a DLQ | Delete |
| `OwnerApiErrorAlarm`, `HoldExpiryErrorAlarm`, `OutboxDispatchErrorAlarm`, `NoteRetentionErrorAlarm`, `SmsRetentionErrorAlarm`, `SmsSenderErrorAlarm`, `SmsConversationErrorAlarm` | `AWS::CloudWatch::Alarm` | Lambda `Errors` over two 5-minute periods, one per function | Delete |

Counts: 7 Lambda functions, 7 IAM roles, 9 Lambda permissions, 4 EventBridge rules (all disabled), 2 event source mappings (both disabled), 4 SQS queues, 11 CloudWatch alarms, 2 metric filters, 7 log groups, 1 table, 1 topic plus policy, 3 Cognito resources, 1 HTTP API plus stage.

Facts worth stating plainly:
- All IAM roles are auto-named by CloudFormation, so `--capabilities CAPABILITY_IAM` suffices (no `CAPABILITY_NAMED_IAM`).
- The template defines **no Lambda alias or version publishing** (`AutoPublishAlias` is unset). Code rollback therefore means redeploying an earlier artifact, not moving an alias.
- No secrets, SSM parameters, KMS keys, VPC resources, or S3 buckets are declared by the template.
- The customer-managed KMS key, the SSM SecureString, and the OpenAI key parameter do not exist in this stack. The conversation and sender roles reference parameter paths that CloudFormation cannot create; that is harmless while everything is disabled.

### 2.2 Resources conditionally absent with these settings

Eight resources exist only when `EnableSmsIngress=true`. They are **not created** for the checkpoint.

| Logical ID | Type | Purpose |
| --- | --- | --- |
| `SmsHttpApi` | `AWS::ApiGatewayV2::Api` | Public signed Twilio webhook API |
| `SmsHttpApiApiGatewayDefaultStage` | `AWS::ApiGatewayV2::Stage` | Its `$default` stage |
| `SmsIngressFunction` | `AWS::Lambda::Function` | `scheduling.sms_lambda.handler` |
| `SmsIngressFunctionRole` | `AWS::IAM::Role` | Table access, read of the Twilio token parameter, send to the conversation queue |
| `SmsIngressFunctionInboundPermission`, `SmsIngressFunctionStatusPermission` | `AWS::Lambda::Permission` | API may invoke the function |
| `SmsIngressLogGroup` | `AWS::Logs::LogGroup` | 30-day retention |
| `SmsIngressErrorAlarm` | `AWS::CloudWatch::Alarm` | Signed webhook Lambda errors (12th alarm) |

Two other things depend on more than one gate rather than on a resource:
- `SmsConversationFunctionReceipts` is created but `Enabled` only when ingress is on, `SmsSendEnabled=authorized`, and `EnableSmsConversations=authorized`. With the dev settings it is disabled.
- The `SmsApiUrl` stack output exists only with ingress on.

### 2.3 Stack parameters

Placeholders are inert. A real value for `AlarmEmail` is supplied on the command line at deploy time and never committed. None of these parameters holds a secret in the dev checkpoint.

| Parameter | Proposed dev value | Secret? | Note |
| --- | --- | --- | --- |
| `Environment` | `dev` | No | Allowed `dev`, `pilot` |
| `BusinessId` | `dev-synthetic` | No | Must equal the Amplify `NEXT_PUBLIC_BUSINESS_ID`. A proposal, subject to review |
| `OwnerSub` | `00000000-0000-0000-0000-000000000000` for the first create, then the owner test user's Cognito `sub` | No (an identifier) | Required, no default |
| `CognitoDomainPrefix` | `<region-unique prefix, lowercase>` | No (public host name) | Required. Must be unused in the region |
| `OwnerAppOrigin` | `https://example.invalid` first, then the exact Amplify origin | No | Pattern `https://host[:port]`, no trailing slash |
| `AlarmEmail` | `<owner-monitored mailbox>` | Personal data; not committed | Required. Recipient must confirm the subscription |
| `PermissionsBoundaryArn` | The role stack's `LambdaRoleBoundaryArn` output | No | Empty by default. **Must be set** when deploying through the execution role, or creating the Lambda roles is denied. Never a real ARN in the repository |
| `EnableSmsIngress` | `false` | No | Default |
| `TwilioAccountSid` | `placeholder` | No | Default. Not a real SID |
| `TwilioBusinessNumber` | `+14155550000` | No | Default; fictional 555 number |
| `OwnerNumber` | `+14155559999` | Sensitive (`NoEcho`) in a real pilot | Default; fictional number |
| `TwilioInboundUrl` | `https://example.invalid/webhooks/sms/inbound` | No | Default |
| `TwilioStatusUrl` | `https://example.invalid/webhooks/sms/status` | No | Default |
| `AuthorizedSmsRecipients` | `` (empty) | Would be personal data | Empty allowlist at the checkpoint |
| `SmsSendEnabled` | `disabled` | No | Default |
| `EnableSmsConversations` | `disabled` | No | Default |
| `HoldExpiryScheduleState`, `NoteRetentionScheduleState`, `SmsRetentionScheduleState` | The schedule's current live state on the first deploy (`ENABLED` for all three on `dev` today) | No | `ENABLED` or `DISABLED`, default `DISABLED`. Pass explicitly with `--param`; see [section 2.5](#25-enabling-schedules-template-parameters-decided-issue-97). Outbox dispatch has no parameter and stays disabled |

**Order of creation** (every step needs the owner's authorization for that specific action; nothing here is authorized by this document):

1. **Identity Center access.** Done by the owner: the `AdministratorAccess` permission set is assigned to the owner's user for account `214965372605`, with MFA at every sign-in.
2. **Budget.** The owner creates the $10 budget in the member account `214965372605`, signed in through Identity Center ([section 1.6](#16-approved-aws-budget-scoped-to-the-dedicated-account)). Done 2026-09-30.
3. **Role stack.** An administrator session creates `infra/dev-deploy-roles.yaml` as its own stack with `--no-execute-changeset`, the owner reviews the change set, then it is executed. See [Dev deployment roles](PILOT_INFRASTRUCTURE.md#create-the-role-stack). The role stack must exist before the dev stack because the dev stack is deployed *through* its roles.
4. **Artifact bucket.** Created by hand (below). The role stack names the bucket, so decide its name before step 3. Create it with the admin session; the deployer role can then use it.
5. **Change set.** `sam deploy --no-execute-changeset` through the deployer role, with `--role-arn` set to the execution role. The owner reviews it.
6. **Execute** the change set, then the rest of the checkpoint: owner test user, Amplify app, the `OwnerSub` and `OwnerAppOrigin` update, the synthetic tests, and the schedule enabling.

Teardown reverses this: the dev stack and its data first, the role stack last (section 3.3).

CLI profiles (kept in `~/.aws/config`, never in the repository): an Identity Center admin profile made with `aws configure sso`, and a deployer profile that assumes the deployer role from it. Details and the trust design are in [Dev deployment roles](PILOT_INFRASTRUCTURE.md#assume-the-deployer-role). Every command below runs with the deployer profile (`--profile scheduling-dev-deployer`) unless it is marked "admin".

Documentation-only shape of the deploy step (do not run without authorization):

```sh
aws sso login --sso-session <session name>   # opens the browser; MFA happens here

# One-time, by hand, admin profile: create a private, versioned, encrypted artifact bucket (decision 3).
aws s3api create-bucket --bucket <artifact bucket> --region us-west-1 \
  --create-bucket-configuration LocationConstraint=us-west-1
aws s3api put-public-access-block --bucket <artifact bucket> \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket <artifact bucket> --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket <artifact bucket> \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

sam build --template-file template.yaml
sam deploy --profile scheduling-dev-deployer --stack-name scheduling-dev --region us-west-1 \
  --role-arn <CloudFormationExecutionRoleArn> \
  --capabilities CAPABILITY_IAM --no-execute-changeset \
  --s3-bucket <dedicated artifact bucket> --s3-prefix scheduling-dev \
  --parameter-overrides Environment=dev BusinessId=dev-synthetic \
    OwnerSub=00000000-0000-0000-0000-000000000000 \
    CognitoDomainPrefix=<unique prefix> OwnerAppOrigin=https://example.invalid \
    AlarmEmail=<owner-monitored mailbox> PermissionsBoundaryArn=<LambdaRoleBoundaryArn>
```

The deployer role's own policy rejects a change set or stack deletion that does not name the execution role, so `--role-arn` is required on both `sam deploy` and `delete-stack`.

This packet uses a **dedicated artifact bucket** that is created by hand first. `sam deploy --s3-bucket` uploads to an existing bucket and does not create one. The alternative, `--resolve-s3`, would create the `aws-sam-cli-managed-default` stack and bucket; it is not used here so that teardown touches only what this checkpoint created.

`--no-execute-changeset` creates the change set for review, but **creating it is not free of side effects**: it needs AWS credentials, it uploads the packaged Lambda code to S3 (a billable, if tiny, artifact), and it creates the stack `scheduling-dev` in `REVIEW_IN_PROGRESS` with no resources. Creating the change set is therefore part of the authorization request (decision 3), not a preliminary step. Executing it is a further authorized act.

### 2.4 Created outside the stack at the checkpoint

| Item | Created by | Billable | Cleanup |
| --- | --- | --- | --- |
| Cognito owner test user | Admin create in the user pool, then set a password and read its `sub` | No (1 MAU) | Deleted with the pool; see section 3.3 |
| Amplify app `scheduling-owner-dev` (platform `WEB`, branch `main`, manual deploy with no Git connection, custom headers from `customHttp.yml`; `NEXT_PUBLIC_*` values from stack outputs plus `NEXT_PUBLIC_BUSINESS_ID=dev-synthetic` are set in the local build, not on the app) | CLI, from the admin session; see the manual deploy note below | Yes: storage and served data (manual deploy uses no build minutes) | `delete-app` |
| AWS Budget `scheduling-dev` (member account `214965372605`, scope all services) and its notification mailbox | The owner, signed in to the member account, first (done 2026-09-30) | No for a plain cost budget | `delete-budget` in the member account |
| SNS subscription confirmation | The `AlarmEmail` recipient clicks the confirmation link | No | Removed with the topic |
| Dedicated artifact bucket (private, versioned, encrypted), created by hand | `aws s3api create-bucket` and related calls | Yes: S3 storage, tiny | Delete all versions and delete markers, then the bucket |
| Uploaded Lambda zips and the `REVIEW_IN_PROGRESS` stack from the change set | `sam deploy --no-execute-changeset` | Yes (storage, tiny) | Removed with the bucket; see the never-executed case in section 3.3 |
| Role stack `scheduling-dev-roles` (deployer role, CloudFormation execution role, Lambda permissions boundary) | An administrator session (Identity Center `AdministratorAccess`), from `infra/dev-deploy-roles.yaml` | No | Deleted last, after everything else (section 3.3, step 12) |
| SSM SecureString `/scheduling/dev/twilio/auth-token` (and `.../openai/api-key`) | Would be created by hand | Not at the checkpoint | **Not created for this checkpoint.** Ingress stays off and no token exists |

**Manual deploy for the `dev` owner app (owner decision, 2026-09-30, #43).** The `dev` owner app is a manual deploy with no Git connection. AWS gets no repository access, pushes trigger no builds (no build minutes), and each update is an explicit upload. Create the app with `aws amplify create-app --name scheduling-owner-dev --platform WEB --custom-headers <customHeaders YAML> --region us-west-1`, then `aws amplify create-branch --app-id <app id> --branch-name main --region us-west-1`. Build locally from `frontend/` with `NEXT_PUBLIC_API_BASE_URL` (`OwnerApiUrl`, no trailing slash), `NEXT_PUBLIC_COGNITO_DOMAIN` (`OwnerCognitoDomain`), `NEXT_PUBLIC_COGNITO_CLIENT_ID` (`OwnerAppClientId`), `NEXT_PUBLIC_BUSINESS_ID=dev-synthetic`, and `NEXT_PUBLIC_AUTH_MODE=cognito`, run `npm run check:export`, and zip the contents of `frontend/out`. Deploy with `aws amplify create-deployment --app-id <app id> --branch-name main --region us-west-1` (returns `jobId` and `zipUploadUrl`), upload the zip to `zipUploadUrl` with an HTTP PUT, then `aws amplify start-deployment --app-id <app id> --branch-name main --job-id <job id> --region us-west-1`. Keep every uploaded zip until teardown, named by the commit it was built from, at `s3://<artifact bucket>/owner-app/<commit>.zip` (admin; the bucket is versioned, private, and emptied at teardown), so a rollback does not depend on rebuilding. A manual app cannot read `customHttp.yml` from a repository, so its headers are applied with `--custom-headers` in the non-monorepo `customHeaders:` format (the `pattern`/`headers` entries of `customHttp.yml` without the `applications`/`appRoot` wrapper); `customHttp.yml` is the source of that value, and their presence is verified after deploy. The origin is `https://main.<app id>.amplifyapp.com`. `amplify.yml` and `customHttp.yml` remain the source for a future Git-connected app, which would be a new Amplify app with a new origin followed by one reviewed `OwnerAppOrigin` change set.

**Scripted since #94.** `scripts/dev/deploy-frontend.sh` runs the steps above (build with the stack outputs, `check:export`, zip, keep the zip in S3, create, upload, start, wait for `SUCCEED`, verify headers), and `scripts/dev/deploy-backend.sh` runs the change-set deploy with a y/N confirmation. See "Deploy to dev with scripts" in `doc/PILOT_INFRASTRUCTURE.md`. The commands in this section remain the reference. The deployer role can run the frontend script only after the owner applies the optional `AmplifyAppId` role-stack change (a reviewed change set); until then use `--profile scheduling-dev-admin`.

### 2.5 Enabling schedules: template parameters (decided, issue #97)

Decision: the state of the hold-expiry, note-retention and SMS-retention schedules is a template parameter (`HoldExpiryScheduleState`, `NoteRetentionScheduleState`, `SmsRetentionScheduleState`; `ENABLED` or `DISABLED`, default `DISABLED`), wired to each rule's `State`. This replaces the out-of-band `aws events enable-rule` toggle for `dev`. The earlier toggle created drift: the first deploy after #93 produced a change set that modified all four rules, which would have reset the three enabled schedules to disabled (nothing was executed).

- **Outbox dispatch and both event source mappings stay hard-coded disabled**, with no parameter. Enabling either stays a separate reviewed change under live-SMS authorization (#91).
- **Changing a schedule** is a deploy: `scripts/dev/schedules.sh enable|disable <LogicalId>` runs `scripts/dev/deploy-backend.sh --param <X>ScheduleState=<STATE>` (a full deploy of the current checkout, so it ships pending code too; it prints the commit and refuses a dirty tree unless `--allow-dirty`) and the owner reviews the change set.
- **Guard.** Before the prompt, `deploy-backend.sh` reads the target `State` of every added, modified or replaced rule from the change set's processed template (`get-template --template-stage Processed`; a `Ref` resolves to the change-set parameter, a missing `State` means `ENABLED`). For the three parameterized rules it compares the live state (`events:DescribeRule`) with that target and refuses, even with `--yes`, if it would change without the matching `--param`. Any other rule (outbox dispatch, a new rule) must target `DISABLED` or the deploy is refused. It prints each rule's logical ID with live and target state.
- **First deploy after this change.** The new parameters are not on the live stack, so pass each with the schedule's current live state (`scripts/dev/status.sh`): `--param HoldExpiryScheduleState=ENABLED --param NoteRetentionScheduleState=ENABLED --param SmsRetentionScheduleState=ENABLED`. Later deploys keep the live values.
- **Emergency stop.** `aws events disable-rule` (section 3.1) stays the procedure for a failed gate; `schedules.sh` is not an emergency tool (it is a full deploy). The rule then differs from its parameter. A deploy that does not modify the rule leaves it alone, and `deploy-backend.sh` prints a warning on every run naming each schedule whose live state differs from its parameter value. A deploy that modifies the rule (an unrelated change can) is refused until the matching parameter is passed with `--param`. Pass `DISABLED` to keep it stopped, then record the stop and set the parameter to match.

SAM's `Schedule` event accepts `State` (`ENABLED` or `DISABLED`) with an intrinsic function; the translated rule carries `State: !Ref <Parameter>` (checked with the SAM translator).

## 3. Rollback and data cleanup

Teardown is no longer the planned next step: on 2026-09-30 the owner decided to keep `dev` running (see [section 5](#5-checkpoint-results-2026-09-30)). This section remains the documented procedure.

All commands below are documentation. `<...>` values are placeholders. Every command touches the AWS account and needs the same authorization as the deployment. Commands run with the deployer profile (`--profile scheduling-dev-deployer`, omitted below for brevity) unless marked "admin" (the Identity Center administrator profile) or "management account".

### 3.1 Stop on a failed safety gate

Any failed gate in the checkpoint (a losing transaction that left partial data, an alarm that does not deliver, an unexpected invocation or message, a cost alert) stops the checkpoint. In this order:

1. **Stop new work.** Disable any schedule that was enabled, and both SQS mappings if any was touched:

   ```sh
   aws events disable-rule --name <rule name> --region us-west-1
   aws lambda update-event-source-mapping --uuid <mapping uuid> --no-enabled --region us-west-1
   ```

   Rule names and mapping UUIDs come from the stack resources. Confirm afterwards that each rule reports `DISABLED` and each mapping reports `Disabled`.
2. **Emergency brake if needed.** Set reserved concurrency to 0 on the offending function. Low-quota accounts may reject this, in which case rely on step 1 and on API Gateway disabling the route:

   ```sh
   aws lambda put-function-concurrency --function-name <function> --reserved-concurrent-executions 0 --region us-west-1
   ```

3. **Capture evidence before changing anything.** Save the failing invocation logs (`aws logs filter-log-events`), the DLQ depth, the alarm history, and the relevant table items (synthetic only). Record the failure on #43.
4. **Revert the Lambda artifact.** The stack has no alias, so redeploy the last known-good commit: check out that commit and run `scripts/dev/deploy-backend.sh` with `--param <X>ScheduleState=DISABLED` for **every schedule stopped in step 1** (otherwise the unchanged parameter can set a stopped rule back to `ENABLED` when the change set modifies it), review the change set and the schedule states it prints, then confirm. A raw `sam deploy --no-execute-changeset --role-arn <CloudFormationExecutionRoleArn>` needs the same `--parameter-overrides PermissionsBoundaryArn=<unchanged> <X>ScheduleState=DISABLED`. If a stack update itself fails, CloudFormation rolls it back automatically (`UPDATE_ROLLBACK_COMPLETE`). If the **first** creation fails, the stack ends in `ROLLBACK_COMPLETE` (or `ROLLBACK_FAILED`, for example after an interrupted event source mapping; see 3.3 step 5) and must be deleted before a retry, and a table already created survives (see the collision note in 3.3). Keep the previous zip in the artifact bucket until teardown.
5. **Roll back the owner app separately** (admin, or the deployer once the `AmplifyAppId` grant from #94 is applied; `scripts/dev/deploy-frontend.sh` redeploys a checked-out commit). Re-upload the last known-good zip from `s3://<artifact bucket>/owner-app/<commit>.zip` (or, if it is missing, rebuild that commit with the same `NEXT_PUBLIC_*` values) with `aws amplify create-deployment --app-id <app id> --branch-name main --region us-west-1`, an HTTP PUT of the zip to `zipUploadUrl`, and `aws amplify start-deployment --app-id <app id> --branch-name main --job-id <job id> --region us-west-1`. The kept zips are removed with the bucket at teardown.
6. **Do not roll data back automatically.** The table is retained. PITR is a last-resort recovery tool: restoring writes a new table, billed for the restored size, and must be planned rather than run reflexively.
7. **Do not replay a DLQ message blindly.** Inspect it first; the outbox record is authoritative.

Decision 4 (answered 2026-09-30): a redeploy of a previously deployed version of the dev stack (step 4) is pre-authorized for this checkpoint. It covers nothing else.

### 3.2 Code rollback without a gate failure

Use the same steps 1 and 4 to 5. Roll back code and data independently, and keep database changes backward-compatible per the architecture.

### 3.3 Full teardown in order

Read first: **CloudFormation deleting the stack does not delete the table**, and the table name `scheduling-dev` is fixed. A retained table with that name blocks a later create of the same stack. Queue and topic names are also fixed. Stack deletion must name the execution role (`--role-arn`), or the deployer role's policy rejects it.

**If the change set was created but never executed**, the stack is empty in `REVIEW_IN_PROGRESS`. Delete the stack (this also removes its change sets), then continue at step 10 for the bucket:

```sh
aws cloudformation delete-stack --stack-name scheduling-dev --region us-west-1 \
  --role-arn <CloudFormationExecutionRoleArn>
```

1. **Stop everything.** Run the section 3.1 step 1 commands. Confirm no schedule is enabled and no queue message is in flight.
2. **Decide about the table's contents.** Confirm it holds only synthetic records:

   ```sh
   aws dynamodb scan --table-name scheduling-dev --select COUNT --region us-west-1
   ```

   Keep it (evidence for #43) or delete it. Keeping costs PITR and storage: about $0.22 per GB-month plus $0.28 per GB beyond 25 GB. Decision 7 (answered 2026-09-30): delete the retained synthetic dev table after the results are recorded on #43.
3. **Delete the Amplify app** (admin: the deployer's optional Amplify grant never covers deleting an app) so it stops serving:

   ```sh
   aws amplify list-apps --region us-west-1
   aws amplify delete-app --app-id <app id> --region us-west-1
   ```

4. **Delete the Cognito test user** (skippable if the stack is about to delete the pool, but explicit is safer if the pool is kept):

   ```sh
   aws cognito-idp admin-delete-user --user-pool-id <pool id> --username <test user> --region us-west-1
   ```

5. **Delete the stack.** This removes the functions, roles, queues, topic and its subscription, pool, domain, client, API, rules, mappings, alarms, metric filters, and the log groups the template declares. It leaves the table.

   ```sh
   aws cloudformation delete-stack --stack-name scheduling-dev --region us-west-1 \
     --role-arn <CloudFormationExecutionRoleArn>
   aws cloudformation wait stack-delete-complete --stack-name scheduling-dev --region us-west-1
   ```

   If it ends in `DELETE_FAILED`, read the failing resource, fix it, and retry; do not use `--retain-resources` blindly.

   One verified case justifies `--retain-resources` (seen 2026-09-30 after a first creation ended `ROLLBACK_FAILED`): an `AWS::Lambda::EventSourceMapping` reported as `DELETE_FAILED` with an access-denied error for a `lambda:*EventSourceMapping` action on `*` (on #43 first `GetEventSourceMapping`, then, once that read was granted, `DeleteEventSourceMapping`), because its creation was interrupted and the mapping does not exist. Take `<physical-id>` (the mapping UUID) and the function's `<function-name>` from `aws cloudformation describe-stack-resources --stack-name scheduling-dev --region us-west-1`. Before retaining, confirm both that `aws lambda get-event-source-mapping --uuid <physical-id> --region us-west-1` returns `ResourceNotFoundException` and that `aws lambda list-event-source-mappings --function-name <function-name> --region us-west-1` returns no mappings. Then retry with the same role and do not widen it:

   ```sh
   aws cloudformation delete-stack --stack-name scheduling-dev --region us-west-1 \
     --role-arn <CloudFormationExecutionRoleArn> --retain-resources <LogicalResourceId>
   ```
6. **Delete the retained table, if step 2 said so.** PITR recovery data is removed when PITR is disabled. Disable it first if you want no recovery data left at all, accepting that this is irreversible. The template sets no deletion protection on the table, so it can be deleted directly:

   ```sh
   aws dynamodb update-continuous-backups --table-name scheduling-dev \
     --point-in-time-recovery-specification PointInTimeRecoveryEnabled=false --region us-west-1
   aws dynamodb delete-table --table-name scheduling-dev --region us-west-1
   aws dynamodb list-backups --table-name scheduling-dev --backup-type ALL --region us-west-1
   ```

   AWS may keep a deletion-time system backup for a table that had PITR; the `list-backups --backup-type ALL` check (which includes system backups) confirms whether any exists. This plan does not rely on that behavior; verify it in the current DynamoDB documentation before authorizing.
7. **Check for stray log groups.** The template's log groups are deleted with the stack. A function invoked after its log group is deleted can recreate one, so check and delete any that remain:

   ```sh
   aws logs describe-log-groups --log-group-name-prefix /aws/lambda/scheduling-dev- --region us-west-1
   aws logs delete-log-group --log-group-name <name> --region us-west-1
   ```

   The `Scheduling/dev` custom metrics cannot be deleted; they stop being billed when nothing publishes to them. Confirm on the first bill after teardown.
8. **SNS subscription.** Nothing to do in AWS: the subscription goes with the topic. The mailbox can remove the confirmation email.
9. **Delete the budget** unless Enrique wants it to persist (member account `214965372605`, signed in through Identity Center; it is deleted here before the account is closed, or is removed with the account's content at permanent closure):

   ```sh
   aws budgets delete-budget --account-id 214965372605 --budget-name scheduling-dev
   ```

10. **Delete the artifact bucket, including every version.** The bucket is versioned, so `aws s3 rm` alone leaves object versions and delete markers behind and the bucket cannot be deleted. List them, delete them in batches (`delete-objects` accepts up to 1,000 keys per call), then delete the bucket:

    ```sh
    aws s3api list-object-versions --bucket <artifact bucket> --output json \
      --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' > versions.json
    aws s3api delete-objects --bucket <artifact bucket> --delete file://versions.json
    aws s3api list-object-versions --bucket <artifact bucket> --output json \
      --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' > markers.json
    aws s3api delete-objects --bucket <artifact bucket> --delete file://markers.json
    aws s3api list-object-versions --bucket <artifact bucket>
    aws s3api delete-bucket --bucket <artifact bucket> --region us-west-1
    ```

    Repeat the list and delete pair until the final `list-object-versions` returns nothing (a `null` query result means there is nothing left of that kind). Keep `versions.json` and `markers.json` out of the repository; they contain only synthetic object keys. Only this dedicated bucket is deleted; no shared SAM-managed bucket is involved.
11. **SSM parameters** (admin: the deployer role has no SSM access). None exist for this checkpoint. If any were created later: `aws ssm delete-parameter --name /scheduling/dev/twilio/auth-token --region us-west-1`.
12. **Delete the role stack last**, from the admin session, not through the deployer role: `aws cloudformation delete-stack --stack-name scheduling-dev-roles --region us-west-1`. CloudFormation needs the execution role to delete the dev stack's resources, so deleting the roles earlier strands the dev stack in `DELETE_FAILED`. Then remove the deployer profile from `~/.aws/config` and confirm no role named `deploy-scheduling-dev-*` remains.

**Ultimate cleanup: closing the member account.** Because `dev` has its own account, closing account `214965372605` eventually removes everything in it, including anything a step above missed, but not immediately. It is the owner's decision and is not part of this plan's authorization. Per the AWS Organizations documentation (checked when this was written), a closed member account shows as CLOSED for up to 90 days, can be reopened during that time, and its content persists until it is permanently closed; re-check the current rules before deciding. Do not treat closure as instant data deletion. The budget lives in the member account and is removed with the account's content at permanent closure (step 9); there is no management-account budget. Prefer the step-by-step teardown while the account will be reused for later checkpoints.

### 3.4 Nothing-billable-remains checklist

Run after teardown. Each check should show nothing for `scheduling-dev`. The deployer role can run only the calls that need the stack's own resources (its policy has no `list-*` calls on most services), so the checklist is split by who can run it. The deployer role and its execution role are deleted in step 12, so run the deployer part **before** step 12.

**Deployer profile** (before the role stack is deleted):

- [ ] `aws cloudformation describe-stacks --stack-name scheduling-dev` reports the stack does not exist (this includes a stack left in `REVIEW_IN_PROGRESS`).
- [ ] `aws dynamodb describe-table --table-name scheduling-dev` reports the table does not exist, and `aws dynamodb list-backups --table-name scheduling-dev --backup-type ALL` shows none (unless kept on purpose).
- [ ] `aws cloudwatch describe-alarms --alarm-name-prefix scheduling-dev` is empty and `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/scheduling-dev-` is empty.
- [ ] `aws sqs get-queue-url --queue-name scheduling-outbox-dev` (and the other three queue names) reports the queue does not exist.
- [ ] `aws s3api list-object-versions --bucket <artifact bucket>` fails because the bucket does not exist, so no versions or delete markers remain.

**Admin profile** (Identity Center administrator session, in `214965372605`):

- [ ] `aws lambda list-functions` has no `scheduling-dev-` function; `aws events list-rules` has no rule from the stack.
- [ ] `aws sns list-topics` has no `scheduling-alarms-dev`, and `aws sqs list-queues --queue-name-prefix scheduling-` is empty.
- [ ] `aws cognito-idp list-user-pools --max-results 60` has no `scheduling-owner-dev`, and the domain prefix is released.
- [ ] `aws apigatewayv2 get-apis` has no owner API.
- [ ] `aws amplify list-apps` has no owner app.
- [ ] `aws dynamodb list-tables` has no `scheduling-dev`.
- [ ] No SSM parameter under `/scheduling/dev/`.
- [ ] After step 12: `aws iam list-roles` shows no role starting `deploy-scheduling-dev-` or `scheduling-dev-`, and `aws cloudformation describe-stacks --stack-name scheduling-dev-roles` reports the stack does not exist.
- [ ] Before the account is closed: `aws budgets describe-budgets --account-id 214965372605` no longer lists the checkpoint budget (or it is kept on purpose).

**Management account** (the owner):

- [ ] The next month's Cost Explorer or bill, filtered to account `214965372605`, shows no line from these services. This catches anything missed above.

## 4. What this packet does not do

As written for the original packet on 2026-09-29:

- When written, it did not deploy and did not contact the AWS account. The resource list came from an offline translator run and the prices came from anonymous public HTTP requests.
- The fake SQS consumer and DLQ harness is `backend/tests/test_dev_outbox_queue.py` (issue #87). Its deployed run is recorded in [section 5.2](#52-what-was-proven). Outbox dispatch stays disabled until live SMS is separately authorized.
- It does not include Twilio, OpenAI, or custom-domain costs.

## 5. Checkpoint results (2026-09-30)

Source: issue #43 comments. All data is synthetic; no addresses, subject IDs, app or API IDs, or passwords are recorded here.

### 5.1 Owner decision (2026-09-30)

Keep the synthetic `dev` environment running indefinitely, within the $10/month budget, as a pre-production test environment. The hold-expiry and retention schedules stay enabled. Outbox dispatch and the SMS sender stay disabled until live SMS is separately authorized. The budget alerts remain the guard. Teardown (section 3) remains documented and available but is no longer the planned next step.

### 5.2 What was proven

| Area | Evidence |
| --- | --- |
| Deployment | Stack `scheduling-dev` has 61 resources, deployed through the scoped deployer and execution roles. SMS was off and schedules were disabled at creation. |
| Alarms | The SNS subscription was confirmed and a synthetic alarm delivery was received. |
| Owner app | Hosted UI sign-in with authorization code and PKCE. Amplify security headers present. CORS allowed from the app origin and denied from a foreign origin. An unauthenticated request returns 401 with the allow-origin header. |
| Owner API | Policy seed, client save, and rejection of a different subject. |
| Transaction races | Against the deployed table, 3 runs, 15 of 15 passed (#80/#81). |
| Due GSIs and IAM | Verified. |
| Hold-expiry proof | Report of 4 examined, 3 expired, 1 stale (#84/#85). |
| Scheduled hold-expiry runs | 0 errors across 2 observed scheduled runs (22:37Z to 22:45Z); about 180 ms warm and 988 ms cold; 92 MB. |
| Retention proof | 7 of 7 passed. Notes deleted 1; SMS bodies 1 and evidence 2. Legally held and unexpired notes, SMS bodies and evidence were kept. Owner partition unchanged at 11 items (#86/#88). |
| Outbox SQS/DLQ gate | 9 of 9 passed; the real sender was never invoked (#87/#89). |

### 5.3 Deviations from the original plan

- A dedicated member account instead of the shared account (#17 superseded for `dev`). The budget lives in the member account.
- Two first-run execution-role permission fixes: the SAM transform (#73) and stage tagging and mapping reads (#74). The failed rollback was recovered without widening the role, by deleting with `--retain-resources` for the one mapping that did not exist. That case is documented in section 3.3 step 5 (#75).
- The Amplify manual deploy (#76).
- App fixes found in testing: the policy seed UI (#77/#78) and hosted UI logout on 401 (#82/#83).
- The Identity Center interactive session is 8 hours, not the recommended 1 to 4.
- The newly installed AWS MCP tool authenticates as a management-account IAM user, so it was not used for changes.

### 5.4 Running cost observations

Read-only reading at 2026-10-01T00:27Z (posted on #43): the AWS Budget `scheduling-dev` shows actual spend of $0.0 USD for the month, and Cost Explorer `UnblendedCost` for 2026-09-30 (the member account's first day) is $0 (estimated). Cost data lags by hours to a day, so this is not a steady-state reading. Expected steady state with these schedules enabled: between $1.14 (scenario (a), idle) and $3.20 (scenario (c), all schedules all month) per month at list price, or $0.11 to $0.75 after always-free allowances (section 1.5).

### 5.5 Rollback and cleanup evidence

- The failed first creation recovery is the rollback evidence: the stack was deleted via the execution role, the retained empty table was deleted with PITR off and 0 backups, and a fresh change set followed.
- The second synthetic test user was deleted after each different-subject check. The owner test user remains.
- Every harness run's leftover sweep returned 0.

### 5.6 Current state

- Enabled: hold expiry (from 2026-09-30T22:37:53Z); note retention and SMS retention (from 2026-09-30T23:56:38Z).
- Disabled: outbox dispatch, both event source mappings, SMS ingress and sending.
- The owner test user exists. The owner app rollback zips are kept under `owner-app/` in the artifact bucket.

### 5.7 Open follow-ups

- #79.
- #67, which is required before `pilot`.
