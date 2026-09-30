# Synthetic `dev` stack: pre-authorization packet

Prepared 2026-09-29 for issue [#43](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/43). This document is **review material only**. It does not authorize, and nothing in it performed, any deployment, AWS account access, spending, owner account creation, or live SMS. The commands are documentation, not instructions to run now. The checkpoint itself is described in [Pilot infrastructure plan](PILOT_INFRASTRUCTURE.md#synthetic-dev-deployment-checkpoint).

Target if authorized: AWS account `339713090487`, region `us-west-1`, stack name `scheduling-dev`, `Environment=dev`, synthetic data only.

## Summary for Enrique

**Nothing is authorized yet.** This document only lays out what a "yes" would mean. No deployment, spending, owner account creation, or live text has happened or will happen without your separate approval.

- **What it would cost per month** (us-west-1, list price, 12-month free tier not assumed): about **$1.14** idle, **$2.43** in an active test month, and **$3.20** in a worst case where every scheduled job ran all month. After the allowances AWS always gives away for free, those become about $0.11, $0.69, and $0.75. Twilio, OpenAI, and taxes are not included.
- **Approved budget (2026-09-30):** a **$10 per month** AWS budget, with email alerts when actual spend reaches $5, $8, and $10 and when AWS forecasts $10. Because the account is shared with other proof-of-concept projects, it is filtered to this project's `Project=scheduling-dev` cost-allocation tag ([section 1.6](#16-approved-aws-budget-scoped-to-this-project)). A budget warns; it does not stop spending.
- **What saying yes to the dev checkpoint would authorize:** (1) creating a private, versioned, encrypted S3 bucket for build artifacts and uploading the build; (2) creating a *change set* (below); (3) executing it, which creates the 61 resources listed in section 2, all with SMS off and every schedule off; (4) creating one owner test user, one Amplify app, and the budget; (5) the checkpoint tests on synthetic data, including switching on the hold-expiry schedule and later the two retention schedules; and (6) the teardown in section 3 afterward.
- **What it would not authorize:** SMS ingress or sending, Twilio, OpenAI, outbox dispatch, any real client data, the `pilot` stack, or rollback redeploys unless you approve those in decision 4. The 2026-09-30 provisioning authorization also does **not** cover creating the deployment role stack (an IAM change that needs its own authorization and comes first, see [section 2.6](#26-order-of-creation)).

Two terms, once. A **change set** is CloudFormation's preview of what a deployment would create; nothing exists until it is executed, but creating it still needs AWS credentials, uploads the build to S3, and leaves an empty stack in `REVIEW_IN_PROGRESS`. **PITR** (point-in-time recovery) is DynamoDB's continuous backup that can restore the table to any second in the last 35 days.

## Decisions Enrique must make

Status, 2026-09-30 (issue #43): decisions 1 to 4 and 7 were answered by the owner; the account was confirmed as **shared with other proof-of-concept projects**; the operator IAM user **now has an MFA device**; account `339713090487` is the **management account** of its own AWS Organization and its only member. Decision 5 is still open. Decision 6 is answered by `infra/dev-deploy-roles.yaml` in this repository, which still needs the owner's separate authorization to create.

1. **Monthly budget amount and alert thresholds.** Approved: $10 per month, see [section 1.6](#16-approved-aws-budget-scoped-to-this-project).
2. **The owner-monitored alarm mailbox** (`AlarmEmail`). No address is recorded in the repository.
3. **Provisioning authorization.** It covers, each a separate billable or account-changing step: creating the artifact bucket (private, versioned, encrypted) and uploading the build; **creating the change set** (uses credentials, uploads billable artifacts, and leaves the stack in `REVIEW_IN_PROGRESS`); executing the change set; creating the owner test user, the Amplify app, and the budget.
4. **Whether rollback redeploys are pre-authorized** as part of this checkpoint, or each one needs a fresh yes (see [section 3.1](#31-stop-on-a-failed-safety-gate)).
5. **How schedules get enabled** (see [section 2.5](#25-technical-question-for-review-enabling-schedules)). The template hard-codes them off and has no parameter to turn one on.
6. **The scoped deployment role.** The checkpoint forbids broad personal administrator credentials. The scoped roles are defined in `infra/dev-deploy-roles.yaml` (see [Dev deployment roles](PILOT_INFRASTRUCTURE.md#dev-deployment-roles)); creating them is a separate IAM authorization; the deployer role requires MFA, and the operator IAM user now has a device.
7. **Whether the synthetic dev table is deleted at teardown or kept for inspection.** The recommendation is to delete it; the decision is Enrique's (see [section 3.3](#33-full-teardown-in-order)).

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

**(a) Idle baseline.** Stack deployed; every EventBridge rule disabled; both SQS mappings disabled; `EnableSmsIngress=false`; Amplify app created, no builds. Traffic: 100 owner API requests (300 ms each), 1,000 read and 200 write request units, 0 monthly active users, 1 MB of logs. No worker emits data, so the two log-derived custom metrics do not exist. The 11 alarms still bill.

**(b) Active verification month.** The checkpoint steps 1 to 6 in the pilot plan, with generous headroom. Synthetic dev table of 5 MB (0.005 GB) with PITR on, and 5 synthetic clients with notes.
- 5,000 owner API requests (the pilot-plan scenario), 300 ms average, 256 MB.
- 200,000 read and 100,000 write request units in total, plus the note scans below. This covers the API calls, three transaction-race families run repeatedly (two approvals for one slot, approval versus expiry, replacement swap versus stale write), and the fake-queue `DispatchService` harness. Transactions cost twice the units of ordinary writes, and each write to an indexed item also writes the GSIs.
- Hold expiry enabled for 7 days at `rate(5 minutes)`: 2,016 runs at 1 s. Note and SMS retention enabled for 7 days after the expiry and legal-hold checks: 14 runs at 5 s (7 of them note retention). Outbox dispatch stays **disabled**, as in the pilot plan. 1,000 extra manual or harness invocations at 1 s.
- **Note scans:** 5 clients with notes x 7 note-retention runs, plus 300 owner note-list or note-create calls = 335 scans. Each scan of a 5 MB table reads about 1,280 read units (5 MB / 4 KB), so 335 x 1,280 = 428,800 read units. See the formula below.
- 10,000 SQS requests (300 synthetic intents through a fake consumer, plus the DLQ exercise; the stack has no consumer and the sender mapping stays disabled).
- 0.1 GB of logs ingested. Both metric filters emit at least once (the alarm-transition checks), so 2 custom metrics bill.
- 1 owner monthly active user. 10 Amplify builds of 4 minutes, 0.05 GB stored, 0.1 GB served. 0.1 GB of API responses out. 10,000 KMS requests for the DynamoDB managed key (an assumption). 0.25 GB in the SAM artifact bucket (several build versions of a Lambda zip, which is a small package: `backend/` is under 1 MB before dependencies).

**(c) Worst case: all four schedules run all month.** As (b), but 30 days of every schedule, using the invocation counts in the pilot plan: hold expiry 8,640, outbox dispatch 43,200, note retention 30 and SMS retention 30. Synthetic dev table of 5 MB with 5 synthetic clients with notes, and 1 GB of logs (deliberate headroom).
- Runs: hold expiry 1 s, outbox dispatch 0.5 s, retention 5 s.
- Each hold-expiry run reads 1 read unit, each outbox run 0.5.
- **Note scans.** 5 clients x 30 note-retention runs + 300 note-list or note-create calls = 450 scans x 1,280 read units = 576,000 read units. SMS retention uses queries and is assumed to cost about 100 read units per run.
- **Cost formula for note retention.** `last_visit_end` (`backend/scheduling/adapters/dynamodb.py`) is a strongly consistent, full-table `Scan`. It runs once per client with notes on every daily note-retention run, and once per owner note list and per note create. Read units per month = (clients with notes x runs + note API calls) x table size / 4 KB. At $0.1395 per million read units, one scan of a 1 GB table (262,144 units) costs about $0.037. The scan bills every item in the table, not only notes, so cost grows with the table.
- **Tracked for a code fix in #67.** That fix must land before real client records go into the `pilot` stack. It does not block the synthetic dev stack, whose table is a few megabytes.
- The outbox queue mapping stays disabled, so messages produced by outbox dispatch would accumulate. Enabling either SQS event source mapping adds continuous long-poll receive requests billed as SQS requests. That is not included. A rough upper bound is about $0.26 per queue-month (about five pollers polling every 20 seconds at the $0.40/million price), which is an estimate to be measured, not a quote.

### 1.5 Monthly totals

| Line | (a) Idle | (b) Active verification | (c) All schedules all month |
| --- | ---: | ---: | ---: |
| Lambda (requests and duration) | $0.00 | $0.02 | $0.12 |
| API Gateway HTTP API | $0.00 | $0.01 | $0.01 |
| DynamoDB request units | $0.00 | $0.16 | $0.18 |
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
| **Total at list price (no allowances)** | **$1.14** | **$2.43** | **$3.20** |
| **Total after always-free allowances** | **$0.11** | **$0.69** | **$0.75** |

Rows are rounded to the cent, so they may not add exactly to the totals, which are computed from unrounded values (for example, (a) also includes about $0.001 of Amplify storage and a few hundredths of a cent of requests).

Reading the table:
- The dominant fixed cost is the **11 CloudWatch alarms** ($1.10 at list; $0.10 after the 10-alarm allowance). Enabling SMS ingress adds a 12th ($0.10).
- The idle stack costs about $1 per month at list price. It is not zero because alarms, the DynamoDB PITR/storage, and Amplify storage bill while idle.
- Log ingestion is $0.67 per GB in `us-west-1`, which is more than the widely quoted `us-east-1` price. Log volume growth is the most likely source of surprise; every extra GB of ingested logs adds $0.67 before the 5 GB allowance.
- Sensitivity, per scan: one full scan of a 10 GB table uses about 2.6 million read units, or about $0.37 **per scan**, and one of a 1 GB table about $0.037. **Pilot scale** with the current code: 20 clients with notes on a 1 GB table means 20 x 30 = 600 scans a month, or about **$22 a month** for daily retention alone, plus $0.037 for every owner note list or note create. That is why #67 must land before the `pilot` stack holds real records. A 10 GB table also adds about $2.2 a month of PITR before the 25 GB storage allowance.
- This estimate sits well below the earlier provisional $10 to $30 planning envelope, which the pilot plan no longer carries.

### 1.6 Approved AWS Budget, scoped to this project

**Approved by Enrique on 2026-09-30 (issue #43): $10.00 per month, actual-spend alerts at $5.00, $8.00 and $10.00, and a forecast alert at $10.00.** These amounts and thresholds are the owner's decision; this section only says how the budget is scoped. It applies to the synthetic `dev` checkpoint. The `pilot` stack needs its own budget after #67 lands, because at pilot scale the current scan costs about $22 a month.

- **Why it is filtered.** Account `339713090487` is **shared with other proof-of-concept projects** (owner answer, 2026-09-30). An unfiltered budget counts the whole account, so other projects' spend would trigger these alerts and hide this stack's. The budget is therefore filtered to this project's cost-allocation tag: **`Project` = `scheduling-dev`** (a user-defined tag; in the Budgets filter it appears as `user:Project$scheduling-dev`). The same tag drives the IAM scoping of HTTP APIs and user pools ([Tag scoping](PILOT_INFRASTRUCTURE.md#tag-scoping-in-a-shared-account)); `template.yaml` sets it on the API and user pool and `sam deploy --tags Project=scheduling-dev` puts it on every other taggable stack resource.
- **Type:** monthly cost budget with no budget actions (actions cost $0.10 per budget-day after 62 days and would need extra IAM).
- **Alerts, sent to the owner-monitored mailbox:** actual spend at 50 percent ($5.00), 80 percent ($8.00) and 100 percent ($10.00); forecasted spend at 100 percent ($10.00).
- **One-time activation, by the owner or administrator, with billing access.** In Billing and Cost Management, Cost allocation tags, activate the user-defined tag key `Project`. A tag key is listed only after at least one resource carrying it exists and has produced usage data, and AWS documents that it can take up to about 24 hours for a new key to appear and up to about 24 hours after activation before it is active; cost data also lags by hours. The account is the management account of its own AWS Organization (its only member), so the activation is done from this account with the owner's credentials. Re-check these timings in the current AWS documentation when activating. Tag the Amplify app and the artifact bucket `Project=scheduling-dev` when creating them, since they are not created by the stack.
- **Order and the gap.** The tag filter can only be chosen after the key is active, so the sequence is: create the stack (tagged), wait for the key to appear, activate it, wait for it to become active, then create the budget. **For up to about two days after the first deploy there is no budget alert at all.** During that gap nothing is alerted on: the tag-filtered budget does not exist yet, and even once it does, spend before activation may not be attributed to the tag. What limits the exposure is that the stack is created idle (every schedule and mapping disabled, no SMS): the list-price idle cost is about $1.14 per month, roughly $0.04 per day, and the always-on alarms and metric filters are the bulk of it. **Decision (manager, 2026-09-30):** the gap is accepted and no temporary account-wide budget is created (it would count other projects' spend and could alert falsely). **Rule: no schedule (checkpoint step 5) and no event source mapping may be enabled until the tag-filtered budget exists and the first tagged cost data appears.**
- **What the tag filter does not see.** Charges that cannot carry the tag fall outside it: data transfer out, some CloudWatch charges (for example metric-filter custom metrics and log ingestion, depending on how AWS attributes them), and anything else AWS reports without resource tags. For this stack they are small (data transfer about $0.01, metrics $0.60 in scenarios (b) and (c) in section 1.5). A complementary filter by service or region is **not** sound here: other projects in the account use the same services and region, so it would alert on their spend. Instead, after the first full month, compare Cost Explorer grouped by the `Project` tag with the untagged remainder ("No tag key") and confirm the untagged part is small; treat any unexplained untagged spend as something to raise with the owner, not something the budget covers.
- **Limits.** A budget alerts; it does not stop spending. Cost data lags by hours, so an alert can arrive after money is spent. The tag filter narrows the budget to this project's tagged resources; it does not protect the account's other spend.

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
- **Amplify Hosting:** 40 build minutes, 0.05 GB stored, 0.1 GB served.
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
| `NoteRetentionFunctionRole` | `AWS::IAM::Role` | Basic execution plus table access including `Scan` | Delete |
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
| `EnableSmsIngress` | `false` | No | Default |
| `TwilioAccountSid` | `placeholder` | No | Default. Not a real SID |
| `TwilioBusinessNumber` | `+14155550000` | No | Default; fictional 555 number |
| `OwnerNumber` | `+14155559999` | Sensitive (`NoEcho`) in a real pilot | Default; fictional number |
| `TwilioInboundUrl` | `https://example.invalid/webhooks/sms/inbound` | No | Default |
| `TwilioStatusUrl` | `https://example.invalid/webhooks/sms/status` | No | Default |
| `AuthorizedSmsRecipients` | `` (empty) | Would be personal data | Empty allowlist at the checkpoint |
| `SmsSendEnabled` | `disabled` | No | Default |
| `EnableSmsConversations` | `disabled` | No | Default |
| `PermissionsBoundaryArn` | The role stack's `LambdaRoleBoundaryArn` output | No (an ARN; never committed) | Required when deploying through the scoped execution role; without it role creation is denied. Empty (the default) means unbounded Lambda roles |
| Stack tag `Project` (not a template parameter; passed as `sam deploy --tags`) | `scheduling-dev` (the stack name) | No | Required: the deployer role's `CreateChangeSet` is denied without it. It is what the scoped roles and the cost budget key on. `template.yaml` also sets it on the HTTP APIs and the user pool from `AWS::StackName`, so the value must equal the stack name |

Documentation-only shape of the deploy step (do not run without authorization). The role stack from [Dev deployment roles](PILOT_INFRASTRUCTURE.md#dev-deployment-roles) exists first ([section 2.6](#26-order-of-creation)). The bucket is created by the owner's own credentials; the deployer role has no `CreateBucket`. Everything from `sam build` on uses the deployer profile (MFA) and passes the execution role:

```sh
# One-time, by hand, owner credentials: create a private, versioned, encrypted artifact bucket
# (decision 3), tagged for the cost budget.
aws s3api create-bucket --bucket <artifact bucket> --region us-west-1 \
  --create-bucket-configuration LocationConstraint=us-west-1
aws s3api put-bucket-tagging --bucket <artifact bucket> \
  --tagging 'TagSet=[{Key=Project,Value=scheduling-dev}]'
aws s3api put-public-access-block --bucket <artifact bucket> \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket <artifact bucket> --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket <artifact bucket> \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# Deployer profile (assumes the deployer role with MFA).
sam build --template-file template.yaml
sam deploy --profile scheduling-dev-deployer --stack-name scheduling-dev --region us-west-1 \
  --role-arn <CloudFormationExecutionRoleArn> --tags Project=scheduling-dev \
  --capabilities CAPABILITY_IAM --no-execute-changeset \
  --s3-bucket <dedicated artifact bucket> --s3-prefix scheduling-dev \
  --parameter-overrides Environment=dev BusinessId=dev-synthetic \
    OwnerSub=00000000-0000-0000-0000-000000000000 \
    CognitoDomainPrefix=<unique prefix> OwnerAppOrigin=https://example.invalid \
    AlarmEmail=<owner-monitored mailbox> PermissionsBoundaryArn=<LambdaRoleBoundaryArn>
```

This packet uses a **dedicated artifact bucket** that is created by hand first. `sam deploy --s3-bucket` uploads to an existing bucket and does not create one. The alternative, `--resolve-s3`, would create the shared `aws-sam-cli-managed-default` stack and bucket; it is not used here so that teardown touches only what this checkpoint created.

`--no-execute-changeset` creates the change set for review, but **creating it is not free of side effects**: it needs AWS credentials, it uploads the packaged Lambda code to S3 (a billable, if tiny, artifact), and it creates the stack `scheduling-dev` in `REVIEW_IN_PROGRESS` with no resources. Creating the change set is therefore part of the authorization request (decision 3), not a preliminary step. Executing it is a further authorized act.

### 2.4 Created outside the stack at the checkpoint

| Item | Created by | Billable | Cleanup |
| --- | --- | --- | --- |
| Cognito owner test user | Admin create in the user pool, then set a password and read its `sub` | No (1 MAU) | Deleted with the pool; see section 3.3 |
| Amplify app (platform `WEB`, `AMPLIFY_MONOREPO_APP_ROOT=frontend`, build variables from stack outputs plus `NEXT_PUBLIC_BUSINESS_ID`), tagged `Project=scheduling-dev` | Console or CLI, owner credentials (the deployer role has no Amplify access) | Yes: build minutes, storage, served data | `delete-app` |
| AWS Budget ($10, tag-filtered) and its notification mailbox, plus the one-time activation of the `Project` cost-allocation tag in Billing | Console or CLI, owner or administrator with billing access; the budget can be created only after the tag is active (section 1.6) | No for a plain cost budget | `delete-budget`; the tag activation can stay |
| SNS subscription confirmation | The `AlarmEmail` recipient clicks the confirmation link | No | Removed with the topic |
| Dedicated artifact bucket (private, versioned, encrypted, tagged `Project=scheduling-dev`), created by hand | `aws s3api create-bucket` and related calls, owner credentials (the deployer role can use and delete the bucket but not create it) | Yes: S3 storage, tiny | Delete all versions and delete markers, then the bucket |
| Uploaded Lambda zips and the `REVIEW_IN_PROGRESS` stack from the change set | `sam deploy --no-execute-changeset` | Yes (storage, tiny) | Removed with the bucket; see the never-executed case in section 3.3 |
| Role stack `scheduling-dev-roles` (deployer role, CloudFormation execution role, Lambda permissions boundary), defined in `infra/dev-deploy-roles.yaml` in this repository | The owner's operator IAM user, first, with `--no-execute-changeset`; the owner reviews the change set, then executes it. Needs the owner's separate IAM authorization | No | Deleted **last**, after everything else (section 3.3 step 12) |
| MFA device on the operator IAM user | The owner, in the IAM console | No | Needed because the deployer role requires MFA. The owner added a device on 2026-09-30; its ARN goes in the deployer profile as `mfa_serial` |
| SSM SecureString `/scheduling/dev/twilio/auth-token` (and `.../openai/api-key`) | Would be created by hand | Not at the checkpoint | **Not created for this checkpoint.** Ingress stays off and no token exists |

### 2.5 Technical question for review: enabling schedules

The template hard-codes `Enabled: false` on every schedule and the sender mapping, and has no parameter to change that. The checkpoint asks to "enable that schedule alone" (hold expiry, then retention). Two ways to do that:
- **Out-of-band toggle** (`aws events enable-rule`). No code change, but it creates drift: a later deploy that leaves the rule's properties unchanged does not reset it, so the state must be recorded and explicitly disabled at stop or teardown.
- **A reviewed template change** adding per-schedule enable parameters, then a deploy.

This packet assumes the out-of-band toggle for the dev checkpoint only, and flags it for review. It is not a business rule, but it changes how the deployed stack differs from the template.

### 2.6 Order of creation

Each step needs the authorization that covers it; nothing here is authorized by this document.

1. **MFA device** on the operator IAM user (done by the owner, 2026-09-30).
2. **Role stack** `scheduling-dev-roles`, by the owner's operator IAM user: create the change set with `--no-execute-changeset`, review the permissions, then execute. This step needs the owner's separate IAM authorization.
3. **Deployer profile** configured with the role stack's outputs; confirm it assumes the role with an MFA code.
4. **Artifact bucket**, tagged, by the owner's credentials.
5. **Dev stack change set** with the deployer profile (`--role-arn`, `--tags Project=scheduling-dev`, `PermissionsBoundaryArn`); review, including every `AssumeRolePolicyDocument` (only `lambda.amazonaws.com`) and that every function's role is one the stack creates; execute with `aws cloudformation execute-change-set --profile scheduling-dev-deployer`.
6. **Cost-allocation tag activation and the tag-filtered budget** (section 1.6), once the tag key appears; then the Amplify app (tagged), the owner test user and the `OwnerSub` / origin update. Keep every schedule disabled until the budget exists.

Teardown reverses this and ends with the role stack (section 3.3).

## 3. Rollback and data cleanup

All commands below are documentation. `<...>` values are placeholders. Every command touches the AWS account and needs the same authorization as the deployment. Unless a step says **owner credentials**, run it with the deployer profile (`--profile scheduling-dev-deployer`), which is what the deployer role's permissions were written for. Stack deletion always passes `--role-arn <CloudFormationExecutionRoleArn>`; the deployer role denies it otherwise.

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
4. **Revert the Lambda artifact.** The stack has no alias, so redeploy the last known-good commit: check out that commit, `sam build`, `sam deploy --profile scheduling-dev-deployer --role-arn <CloudFormationExecutionRoleArn> --tags Project=scheduling-dev --no-execute-changeset` (same parameters as the original deploy), review the change set, then execute it. If a stack update itself fails, CloudFormation rolls it back automatically (`UPDATE_ROLLBACK_COMPLETE`). If the **first** creation fails, the stack ends in `ROLLBACK_COMPLETE` and must be deleted before a retry, and a table already created survives (see the collision note in 3.3). Keep the previous zip in the artifact bucket until teardown.
5. **Roll back the owner app separately.** In the Amplify console redeploy the previous successful build, or disconnect the branch.
6. **Do not roll data back automatically.** The table is retained. PITR is a last-resort recovery tool: restoring writes a new table, billed for the restored size, and must be planned rather than run reflexively.
7. **Do not replay a DLQ message blindly.** Inspect it first; the outbox record is authoritative.

Open decision (item 4 above): whether step 4 counts as pre-authorized for this checkpoint.

### 3.2 Code rollback without a gate failure

Use the same steps 1 and 4 to 5. Roll back code and data independently, and keep database changes backward-compatible per the architecture.

### 3.3 Full teardown in order

Read first: **CloudFormation deleting the stack does not delete the table**, and the table name `scheduling-dev` is fixed. A retained table with that name blocks a later create of the same stack. Queue and topic names are also fixed.

**If the change set was created but never executed**, the stack is empty in `REVIEW_IN_PROGRESS`. Delete the stack (this also removes its change sets), then continue at step 10 for the bucket:

```sh
aws cloudformation delete-stack --stack-name scheduling-dev --region us-west-1 \
  --role-arn <CloudFormationExecutionRoleArn> --profile scheduling-dev-deployer
```

1. **Stop everything.** Run the section 3.1 step 1 commands. Confirm no schedule is enabled and no queue message is in flight.
2. **Decide about the table's contents.** Confirm it holds only synthetic records:

   ```sh
   aws dynamodb scan --table-name scheduling-dev --select COUNT --region us-west-1
   ```

   Keep it (evidence for #43) or delete it. Keeping costs PITR and storage: about $0.22 per GB-month plus $0.28 per GB beyond 25 GB. Decision (Enrique, 2026-09-30): delete the retained synthetic table after the results are recorded on #43.
3. **Delete the Amplify app** (owner credentials; the deployer role has no Amplify access) so it stops building and stops serving:

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
     --role-arn <CloudFormationExecutionRoleArn> --profile scheduling-dev-deployer
   aws cloudformation wait stack-delete-complete --stack-name scheduling-dev --region us-west-1 \
     --profile scheduling-dev-deployer
   ```

   If it ends in `DELETE_FAILED`, read the failing resource, fix it, and retry; do not use `--retain-resources` blindly.
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
9. **Delete the budget** (owner credentials) unless Enrique wants it to persist:

   ```sh
   aws budgets delete-budget --account-id <account id> --budget-name <budget name>
   ```

10. **Delete the artifact bucket, including every version** (deployer profile; it has the object, version and bucket delete permissions for this bucket only). The bucket is versioned, so `aws s3 rm` alone leaves object versions and delete markers behind and the bucket cannot be deleted. List them, delete them in batches (`delete-objects` accepts up to 1,000 keys per call), then delete the bucket:

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
11. **SSM parameters.** None exist for this checkpoint. If any were created later (owner credentials; the roles have no SSM access): `aws ssm delete-parameter --name /scheduling/dev/twilio/auth-token --region us-west-1`.
12. **Delete the role stack last.** Only after steps 1 to 11 (the execution role must exist to delete the dev stack, and the deployer role to empty the bucket). With the owner's operator IAM user, not the deployer role: `aws cloudformation delete-stack --stack-name scheduling-dev-roles --region us-west-1`. Then remove the operator's `scheduling-dev-deployer` CLI profile and confirm no role named `deploy-scheduling-dev-*` remains. The details, including what to do if the event source mapping delete is denied, are in [Teardown order](PILOT_INFRASTRUCTURE.md#teardown-order). The `Project` cost-allocation tag activation can stay; it is not a resource.

### 3.4 Nothing-billable-remains checklist

Run after teardown steps 1 to 11, before the role stack is deleted (step 12), so the deployer profile still works. The deployer role cannot make list calls (`ListTables`, `ListFunctions`, `ListRules`, `ListQueues`, `ListTopics`, `ListUserPools`, `GET /apis`), so the checks are split by who can run them. Each should return nothing, or "not found", for `scheduling-dev`.

**With the deployer profile** (`--profile scheduling-dev-deployer`; calls it can make):

- [ ] `aws cloudformation describe-stacks --stack-name scheduling-dev` reports the stack does not exist (this includes a stack left in `REVIEW_IN_PROGRESS`). If it answers AccessDenied instead of "does not exist", the owner runs it.
- [ ] `aws dynamodb describe-table --table-name scheduling-dev` reports the table not found (unless kept on purpose), and `aws dynamodb list-backups --table-name scheduling-dev --backup-type ALL` shows none.
- [ ] `aws sqs get-queue-url --queue-name <name>` reports the queue does not exist for each of `scheduling-outbox-dev`, `scheduling-outbox-dlq-dev`, `scheduling-sms-conversation-dev` and `scheduling-sms-conversation-dlq-dev`.
- [ ] `aws sns get-topic-attributes --topic-arn <AlarmTopicArn>` reports the topic not found.
- [ ] `aws cloudwatch describe-alarms --alarm-name-prefix scheduling-dev` is empty, and `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/scheduling-dev-` is empty.
- [ ] `aws s3api head-bucket --bucket <artifact bucket>` reports the bucket does not exist (no versions or delete markers remain).
- [ ] The stack's delete events (`aws cloudformation describe-stack-events`, before the stack disappears from the list, or the `wait stack-delete-complete` result) show every function, rule, event source mapping, pool, domain, client and API as `DELETE_COMPLETE`. This stands in for the function and rule list calls, which need names the deployer cannot list.

**With the owner's or an administrator's credentials** (the deployer role has no permission for these):

- [ ] `aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=scheduling-dev --region us-west-1` returns no resource (or only what was kept on purpose). This one sweep covers every taggable resource that carries the project tag, including anything created outside the stack, such as the Amplify app and the bucket.
- [ ] `aws lambda list-functions` has no `scheduling-dev-` function; `aws events list-rules --name-prefix scheduling-dev` has no rule from the stack.
- [ ] `aws cognito-idp list-user-pools --max-results 60` has no `scheduling-owner-dev`, and `aws cognito-idp describe-user-pool-domain --domain <prefix>` shows the domain prefix is released. Filter by name: the account has other projects' pools.
- [ ] `aws apigatewayv2 get-apis` has no owner API (again, only this project's; other projects' APIs are expected).
- [ ] `aws amplify list-apps` has no owner app.
- [ ] `aws budgets describe-budgets --account-id <account id>` no longer lists the checkpoint budget (or it is kept on purpose).
- [ ] No SSM parameter under `/scheduling/dev/`.
- [ ] The next month's Cost Explorer, grouped by the `Project` tag, shows no line for `scheduling-dev`, and the untagged remainder shows nothing attributable to this stack. This catches anything missed above.

## 4. What this packet does not do

- It does not deploy, and it did not contact the AWS account. The resource list came from an offline translator run and the prices came from anonymous public HTTP requests.
- It does not claim the fake SQS consumer and DLQ harness exists. That is a separate reviewed piece of work that must land before outbox dispatch is enabled.
- It does not include Twilio, OpenAI, or custom-domain costs.
