# Technical Architecture: Smart Scheduling Assistant

**Status:** Proposed architecture for review  
**Version:** 0.1  
**Date:** 2026-09-25  
**Related documents:** [PRD](PRD.md), [High-Level Design](HLD.md)

## 1. Purpose

This document makes the AWS deployment and technology choices for the SMS-first scheduling MVP. It uses the existing **NeuroSpineDx** project as a reference for the team's AWS/serverless patterns, but adapts the choices to a small, low-traffic scheduling workload and its most important correctness constraint: no overlapping appointments or active holds.

The goal is low idle cost and low operational overhead—not a large-scale SaaS platform. External services such as SMS and the language-model API remain replaceable integrations. All application-owned compute, data, web hosting, identity, secrets, and logs are deployable on AWS.

## 2. Recommended stack at a glance

| Concern | Recommendation | Reason |
|---|---|---|
| Owner calendar/admin UI | Next.js + React + TypeScript, static export, hosted on AWS Amplify Hosting | Matches NeuroSpineDx frontend experience; static assets avoid always-on web servers/SSR compute |
| Backend API | Python 3.12+ + FastAPI + Mangum, one AWS Lambda function (ZIP package) | Matches NeuroSpineDx backend language/framework; scales to zero and is inexpensive at pilot traffic |
| API ingress | Amazon API Gateway **HTTP API** | Managed webhook/API endpoint, lower-cost API Gateway option for this simple REST interface |
| Primary data store | Amazon DynamoDB, on-demand (PAY_PER_REQUEST) | No provisioned database/idle capacity; suitable for a single small-business calendar |
| Owner UI authentication | Amazon Cognito User Pool, owner account; no SMS-based Cognito MFA | Avoids custom password/session implementation; keep Cognito SMS charges out of the owner-login path |
| SMS | Twilio (external provider), behind an internal SMS adapter | Mature inbound/outbound messaging and delivery callbacks; provider can be replaced later |
| LLM | OpenAI API (external), behind a model adapter; choose a low-cost tool-capable model after testing | Follows the team's existing hosted-OpenAI pattern; no model servers to operate |
| Secrets | AWS Systems Manager Parameter Store, Standard SecureString parameters for provider credentials; compare Secrets Manager if rotation is needed | Keeps fixed secret-management costs low for a tiny pilot; KMS encryption and narrow IAM permissions |
| Notifications/retries | Amazon SQS Standard queue + dead-letter queue; Lambda consumer | Durable outbound SMS retries without an always-running worker |
| Hold expiry | EventBridge scheduled rule invokes a small expiry handler periodically; status is also checked synchronously on every read/write | No server is left running; overdue holds cannot remain bookable merely because a scheduled job is delayed |
| Logs/metrics | Amazon CloudWatch Logs and basic metrics/alarms | Native AWS operations; explicitly set retention to bound log cost and avoid sensitive message logging |
| Infrastructure/deploy | AWS SAM template + GitHub Actions OIDC; Amplify GitHub integration for frontend | Declarative repeatable AWS resources; avoids long-lived AWS keys in GitHub |
| AWS region and accounts | `us-west-1`. The synthetic `dev` stack targets the dedicated member account `214965372605`; the management account `339713090487` is for billing and organization administration only; `pilot` will get its own separate account | Region confirmed in issue #17; the dev account decision (issue #43) supersedes #17's single shared account. No resources are provisioned by this decision |

### Stack continuity with NeuroSpineDx

The inspected repository currently uses Next.js/React, Python/FastAPI, Amplify Hosting, Lambda, API Gateway HTTP API, DynamoDB on-demand, AWS secrets, CloudWatch, and GitHub Actions OIDC. Its latest backend deployment uses a Lambda container image in ECR and scripts to create/update resources. This project retains the core serverless pattern and language choices, but recommends a Lambda ZIP package and SAM-managed infrastructure for the smaller service to avoid container registry/build steps unless dependencies or team preference justify the image path.

## 3. Context and topology

```text
 Client phones                                  Owner phone
      |                                               ^
      | SMS                                           | SMS approval/alerts
      v                                               |
+----------------------+    HTTPS webhook    +--------+---------+
| Twilio (external)    |-------------------->| API Gateway      |
| number, carrier SMS  |                     | HTTP API          |
+----------------------+                     +--------+---------+
                                                       |
                                                Cognito authorizer
                                                for owner routes
                                                       |
                                                       v
                                              +--------+---------+
                                              | FastAPI on Lambda |
                                              | Python + Mangum   |
                                              +---+-----------+---+
                                                  |           |
                             transactional reads/writes       | bounded LLM calls
                                                  |           v
                                                  |      +-----------+
                                                  |      | OpenAI API|
                                                  |      | external  |
                                                  |      +-----------+
                                                  v
                                         +--------+----------+
                                         | DynamoDB on-demand|
                                         | schedule + clients|
                                         +-------------------+
                                                  |
                                      transactional outbox / jobs
                                                  v
                                  +---------------+--------------+
                                  | SQS + DLQ / scheduled expiry |
                                  +---------------+--------------+
                                                  |
                                                  v
                                           Lambda worker
                                                  |
                                                  +----> Twilio outbound SMS

 Owner browser --> Amplify-hosted static Next.js app --> HTTP API
                         |
                         +--> Cognito sign-in (owner only)
```

### AWS-owned components

- Amplify Hosting static frontend.
- API Gateway HTTP API.
- FastAPI Lambda function(s) and IAM roles.
- DynamoDB table(s), SQS queue/DLQ, EventBridge schedule.
- Cognito User Pool for owner web access.
- Parameter Store SecureString values (with KMS encryption).
- CloudWatch logs/metrics/alarms.
- GitHub Actions OIDC role for deployment.

### External dependencies

- Twilio for the SMS phone number, inbound SMS, outbound messages, and delivery status.
- OpenAI API for message interpretation and response drafting; select the production model after the issue #24 evaluation.

The core calendar, appointment state, approval policy, and availability calculation stay in AWS and do not depend on either external provider being available.

## 4. Application components

### 4.1 Owner web application

- Next.js, React, TypeScript; mobile-first because the owner may mostly use a phone.
- Build with static export and host via Amplify Hosting (platform `WEB`). No Next.js SSR or server actions in the initial design; all data operations use the API.
- Implemented in `frontend/` with the Pages Router, whose static export emits no inline scripts, so a build-time CSP `<meta>` tag can allow only self-hosted scripts and connections to the exact API and Cognito origins. Amplify adds `frame-ancestors`, HSTS, and related headers from the repository-root `customHttp.yml`.
- The owner API is cross-origin from the app. API Gateway CORS allows only the configured app origin, without credentials; owner routes use explicit methods so preflight never reaches the JWT authorizer.
- Cognito signs in the owner. API Gateway validates the owner access token for admin routes. The app keeps the `openid` scope and reads the `email` claim from the ID token returned by the token exchange only to show who is signed in (display only, signature not checked, only the string kept in memory, never sent to the API). The claim is expected because the app client can read `email`; this is not yet verified against the live pool, and the app shows "Signed in" when it is absent.
- Screens: day/week schedule, pending approvals, client list/profile, unavailable blocks, and a small settings screen.
- Client-facing booking portal is not part of the MVP; the client workflow is SMS.

### 4.2 API/backend

- FastAPI app served through Mangum from AWS Lambda.
- One backend codebase; route groups have distinct authorization. A single Lambda deployment is sufficient initially. A separate worker Lambda may use the same package only if background responsibilities justify it.
- Route categories:
  - `POST /webhooks/sms/inbound` — provider-signed inbound messages.
  - `POST /webhooks/sms/status` — delivery status callbacks.
  - `/v1/owner/*` — authenticated owner schedule, client, configuration, and pending-request operations.
  - Internal worker entry points for SQS messages and scheduled hold expiry.
  - `/health` — non-sensitive readiness check.
- Use Pydantic request/response schemas, explicit validation, typed domain services, and idempotency keys.
- Organize modules around scheduling domain rules (availability, duration, buffer, transitions) and adapters (DynamoDB, Twilio, model API); do not let provider SDK calls own business rules.

### 4.3 Conversation and AI layer

- Use the model to classify likely intent, extract possible date/time preferences, ask questions, and draft conversational text.
- Use a strict tool interface such as `list_available_slots`, `create_pending_request`, `list_client_appointments`, `cancel_appointment`, `owner_decide_request`, and `save_note_draft`.
- The backend validates all tool arguments and permissions. The model cannot write DynamoDB, send arbitrary messages, or approve an appointment by itself.
- Deterministic scheduling code is authoritative for availability, hold creation/expiration, conflict detection, status transitions, and confirmation messages.
- Start with owner approval for every booking. Automatic approval remains a later product mode and should require an explicit business setting plus pilot evidence.
- Use small bounded context (current request state, relevant upcoming visits, and explicitly approved notes); do not send the full conversation history or credentials to the model.
- If model call fails or confidence is insufficient, ask a clarifying question or route to owner; the calendar remains usable.

### 4.4 SMS adapter

- Normalize E.164 phone numbers; map incoming caller number to a client record or a controlled unknown-client flow.
- Verify Twilio webhook signatures before processing.
- Keep the provider API behind an interface so SMS provider-specific webhook payloads and response codes do not leak into the scheduling domain.
- Record provider message IDs and delivery state; deduplicate inbound retries.
- Support provider-required STOP/HELP and business consent/opt-out rules before production messages are sent.
- Restrict owner actions to the configured owner number and the specific pending request. If a natural reply such as “yes” is ambiguous, ask a question and perform no state change.

## 5. Data architecture and conflict correctness

### 5.1 DynamoDB design

Use a small number of tables and access-pattern-first keys. A single table is a reasonable MVP option:

- Business partition key: `PK = BUSINESS#<business_id>`.
- Calendar records have sort keys beginning `EVENT#<UTC-start>#<event_id>`; query the business partition by a bounded time range and use strongly consistent, fully paginated base-table reads for authoritative availability. The event is a compact projection of the appointment or block, including start/end, status, hold expiry, duration/buffer snapshots, and version.
- Owner unavailable blocks also have authoritative `PK = BUSINESS#<business_id>, SK = BLOCK#<block_id>` records. Strongly query and fully paginate the `BLOCK#` prefix for every availability check, then filter interval intersections in memory. This small-business read avoids assuming a maximum block length: a vacation block that began before the appointment lookback still excludes the slot. A block's calendar event is a UI projection, not the only conflict source.
- Appointment metadata may be stored at `PK = APPOINTMENT#<appointment_id>, SK = META` and written transactionally with its calendar event record.
- Client records use `PK = BUSINESS#<business_id>, SK = CLIENT#<client_id>`; client-phone lookup can use a GSI or a dedicated phone-index item. Treat GSI reads as non-authoritative for writes because GSIs are eventually consistent.
- Store a per-business calendar revision item: `PK = BUSINESS#<business_id>, SK = CALENDAR#REVISION`.
- Owner booking-outreach settings use `PK = BUSINESS#<business_id>, SK = SETTINGS#BOOKING_OUTREACH`, with a conditional version and separate audit record. An absent record reads as disabled and unconfigured. The authenticated owner API and a future scheduled selector use the same repository read; a settings save does not start a worker or send SMS (#250).
- The invitation selector writes `INVITATION#<deterministic-id>` and `INVITATION_GUARD#<sha256(client_id)>` under the business partition, conditionally on settings, calendar revision, verified profile, current in-person consent, no opt-out, and no client-erasure fence. The guard permits one queued intent per client and checks the last successful handoff cutoff. The intent contains run/window instants, client ID, phone hash, and verification instant but no phone or message text. A gated worker promotes one eligible intent to `OUTBOX#booking-invitation#<id>` and the ordinary delivery index. The sender conditionally claims the intent before provider handoff and records terminal sent or suppressed state; uncertain acceptance leaves a held claim for reconciliation. Client erasure removes intent and guard. The EventBridge schedule and invitation delivery gate default to disabled (#251, #252).
- A separate `OutboxDueIndex` GSI uses `outbox_due_pk = OUTBOX#<PENDING|RETRYABLE|SENDING>` and `outbox_due_sk = <dispatch_after UTC>#<outbox_id>`. It can project only base-table keys. The dispatcher queries all three due states with pagination, then strongly rereads each base-table record; the eventually consistent GSI is only a wake-up index, never a source of truth for delivery ownership.
- Keep separate records for appointments, unavailable blocks, client-level notes, appointment-level notes, conversation state, outbox events, and audit events. Exclude entry/access codes from the MVP data model.
- Confirmed appointments also have a small base-table item under `PK = VISITS#<business_id>#<sha256(client_id)>`, `SK = END#<end_at UTC>#<appointment_id>`. Confirmation and owner creation insert it; cancellation and rescheduling remove or move it in the same calendar transaction. Note retention uses a strongly consistent reverse query bounded by the current time with `Limit=1`, then conditionally deletes notes only if the calendar revision is unchanged. This keeps visit lookup cost independent of table size and prevents a concurrent visit transition from authorizing a stale purge. No backfill is needed before real client records exist: the existing `dev` stack contains synthetic data only. Recreate any synthetic historical visits needed for retention tests after this change; backfill this collection before any deployment that already contains real confirmed visits.

Partitioning by business prepares the data model for additional businesses without introducing a multi-region or sharded system. The initial deployment is still one business and one schedulable crew/resource.

### 5.2 No-double-booking strategy

DynamoDB does not provide SQL range-exclusion constraints. Do not implement availability as a read-then-write sequence with no concurrency protection.

Recommended low-volume single-business strategy:
1. Strongly read the business calendar revision **before** reading any calendar events, blocks, or scheduling policy. Every calendar-affecting write, including an operating-hours or holiday edit, must advance this revision in its transaction. Reading the revision after events can combine stale events with a new revision and permit a conflicting write.
2. Strongly read the persisted policy. Require finite `maximum_visit_minutes` and `maximum_buffer_minutes` before booking writes. For a candidate `[start, end)`, query visit/hold events starting at or after `start - maximum_visit_minutes - maximum_buffer_minutes` and before `end + maximum_buffer_minutes`, with strong consistency and all pages. Reject a new visit or buffer above those caps. This window includes an earlier long visit and a later adjacent visit; never cap the number of returned events. Query every calendar day touched by the window if the event index is later partitioned by day. Separately, strongly query all block records under the `BLOCK#` prefix; do not apply the visit lookback cap to blocks. A policy edit that lowers either cap must be rejected while an active confirmed visit or pending hold has a larger stored duration/buffer snapshot; otherwise the lookback could miss it.
3. Calculate availability from the visit/hold events and authoritative blocks. Ignore pending holds with `hold_expires_at <= now` even if their expiry job has not run. For two visits, require a gap of at least `max(candidate.buffer_minutes, existing.buffer_minutes)`; do not add a gap at the first/last working-hours boundary or around an unavailable block.
4. Use one `TransactWriteItems` operation to conditionally advance the revision **from step 1**, conditionally write/update appointment metadata and calendar event, and put the idempotency result, audit event, and required outbox records. Keep below DynamoDB transaction size/item limits; reject an operation that cannot fit instead of splitting the atomic change.
5. If the revision condition fails, start again at step 1 and retry a bounded number of times. A changed calendar may make the requested slot unavailable; return a conflict with fresh alternatives. Replays with the same actor, operation, and idempotency key return the stored result; a reused key with a different payload is an error.
6. Apply this discipline to hold creation, owner approval/decline, expiry, cancellation, block creation/editing, policy edits, owner appointment edits, and reschedule swaps. A replacement request may overlap only its own original appointment during validation. Approval checks all *other* active reservations and atomically cancels the old event while confirming the replacement. The original remains untouched on decline, expiry, or failed approval. When the original is a still-pending request replaced by an accepted owner counteroffer (#176), approval resolves it as declined and releases its event in the same transaction, and the original cannot itself be approved while that replacement is active.

This serializes competing changes to a small business calendar through optimistic concurrency without running a lock server. Keep transactions small and test overlapping requests under concurrency. If volume or multi-crew scheduling grows, revisit resource partitioning and a relational database with exclusion constraints.

Approval also checks `hold_expires_at > decision_at` against the scheduling service's fresh UTC time and the expected pending version. Because time passes without a revision write, use a fresh `decision_at` immediately before submitting the transaction, not the earlier availability-read time. The expiry worker uses the opposite expiry condition and cannot overwrite an approval. A conflicting owner duration increase or move leaves the confirmed appointment unchanged and emits no change notice.

### 5.3 Time, duration, and buffer

- Store instants as UTC epoch/time values; pilot business timezone is the IANA identifier `America/Los_Angeles`, including daylight saving.
- Interpret client local-date phrases in the business timezone; test daylight-saving transitions.
- Small/medium/large home-size categories have configurable pilot defaults of 1/2/3 hours and a current 3-hour maximum. Snapshot duration on each appointment; allow an owner override even after approval.
- Appointment slots must fit the entire duration within business hours and must not overlap confirmed events, active pending holds, or unavailable blocks.
- Default fixed travel buffer is 30 minutes between visits only, with none before the first or after the last. Keep it configurable; no route-aware estimation in the MVP.
- Monday–Friday, 8:00 a.m.–5:00 p.m. is the pilot baseline. Close observed US federal holidays by default using the [OPM holiday schedule](https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/), with owner-editable date exceptions. Holiday edits use the same calendar revision transaction as blocks so an old availability result cannot authorize a conflicting booking; reject a new closure that conflicts with an existing confirmed visit until the owner moves or cancels that visit explicitly.
- The owner policy service seeds these approved defaults once into `POLICY#SCHEDULING` before production booking writes. Edits require the expected policy version and calendar revision. It checks every future active confirmed visit and pending hold against new hours, exceptions, duration and buffer caps, and visit gaps; a conflicting edit leaves the prior policy intact. The atomic write advances the calendar revision and stores the policy version, idempotency result, audit, and owner notice.
- The owner Schedule Day and Week views draw each saved date exception from the policy the app already loads: a closed date is shaded "Closed (date exception)" for the whole day, and custom hours shade that weekday's normal weekly hours outside the exception's open windows (nothing if none remain). These shaded ranges are not calendar events: they are not appointments or manual blocks and cannot be selected. Observed federal holidays come from the holiday calendar setting, not date exceptions, and are not shaded.
- Owner block create/move/remove commands use `BLOCK#<block_id>` records with expected block versions and the calendar revision. Create and move reject overlap with active reservations. Owner-created confirmed appointments use the same bounded strong-read availability check as holds, then atomically write appointment metadata and event plus idempotency, audit, and owner/client notice intents. The authenticated command API for these services is tracked in issue #18.
- No dynamic address-based routing in MVP.

### 5.4 Holds and expiry

- Pending request stores `hold_expires_at` in UTC. Availability code treats a hold as inactive as soon as `hold_expires_at <= now`, regardless of whether cleanup has run.
- Pending appointment metadata carries `hold_due_pk = HOLD#PENDING` and `hold_due_sk = <expiry UTC>#<hold_id>` for a sparse `HoldDueIndex` GSI projecting the base `PK` and `SK`. A terminal transition removes those attributes in the same transaction. The scheduled worker queries a bounded, paginated due batch, then strongly rereads each base appointment because the index is eventually consistent.
- EventBridge invokes a lightweight expiry job periodically (e.g. every 5–15 minutes, exact cadence TBD) to transition expired holds and enqueue client notices.
- The expiry job uses a stable per-hold/version idempotency key and the existing lifecycle transaction. Approval and expiry use conditional status/version writes; approval also requires `hold_expires_at > now`. A late approval cannot confirm an expired request, even before cleanup. Stale index entries and lost approval races are skipped; unexpected errors fail the invocation for EventBridge retry. The worker logs examined, expired, stale, and oldest-overdue metrics. Deployment must configure retries and alert on repeated failures or growing overdue age (issue #23).
- DynamoDB TTL can be used only to clean up disposable records after their retention period; TTL is asynchronous and must never be relied on for availability or exact expiration timing.

## 6. Notifications and reliability

- For every committed state change, transactionally write one immutable notification intent per recipient/template with a stable `outbox_id`, event version, and `PENDING` delivery state. The state write and outbox puts are one DynamoDB transaction. No direct SQS send is required in that transaction.
- An EventBridge-triggered dispatcher queries the due-work GSI for due `PENDING`, `RETRYABLE`, and lease-expired `SENDING` records and sends `business_id` plus stable `outbox_id` to SQS. After a successful send, it conditionally pushes `dispatch_after` forward to avoid queue floods; `next_attempt_at` remains due so the consumer can claim the record. A periodic sweep repeats this query, so a crash after the database commit, a missed invocation, or a worker crash after claim cannot strand the record. GSI lag delays dispatch but cannot lose the record. A crash after `SendMessage` may enqueue a duplicate; the consumer always rereads the authoritative outbox item.
- The SQS consumer conditionally claims a due record with a short lease and attempt number, setting `SENDING` and `next_attempt_at` to the lease expiry in the same conditional update. It marks `SENT` with provider ID on success, or records error, backoff, and a `RETRYABLE` due time on failure. An expired `SENDING` lease can be reclaimed up to the attempt limit; a later recovery marks it `FAILED` without calling the provider. Duplicate queue messages that cannot claim do nothing. A provider timeout after accepting a message can still produce a duplicate SMS on retry unless the provider supports an idempotency key; surface this limit and keep messages status-safe.
- Configure SQS redrive to a DLQ and Lambda partial-batch failures so malformed messages or unexpected worker errors are retried without replaying successful batch members. The provider-independent consumer uses bounded exponential backoff for expected delivery failures and marks exhausted intents `FAILED`; unexpected errors are returned to SQS for redrive. Alarm on oldest due outbox age, expired leases, delivery failures, and DLQ depth. A DLQ entry does not delete its outbox record: operators can inspect and replay by resetting its due time after correcting the cause. Queue/DLQ resources and alarms are part of the pilot infrastructure issue #23.
- Persist booking state before telling either party that it changed.
- SMS delivery failure does not roll back the appointment. Exhausted attempts mark the outbox `FAILED` for visible owner follow-up, with redacted error details.
- Webhook handler acknowledges only after durable message/event persistence. Deduplicate by provider event ID.

## 7. Security and privacy

- Public internet API, no Lambda VPC for MVP. Lambda uses IAM-authorized AWS service endpoints and outbound HTTPS to Twilio/OpenAI. Avoiding a NAT Gateway removes a potentially disproportionate always-on network cost. Reassess VPC only for a concrete private-resource requirement.
- API Gateway Cognito authorizer on owner routes. Restrict inbound SMS endpoints to valid provider signatures; use rate limits/WAF only if abuse or traffic warrants its cost.
- Owner SMS approvals are allowlisted by phone and resolved against pending request state; ambiguous approvals are rejected pending clarification.
- Least-privilege Lambda execution role scoped to required DynamoDB keys/table, SQS queue, Parameter Store names/KMS key, and CloudWatch logs.
- Store third-party keys as SecureString parameters; retrieve/cache at runtime. Never place keys in source, frontend bundles, model prompts, or logs.
- Do not log raw SMS body, access code, full address, or model prompt by default. Use message/request IDs and redacted structured metadata for diagnostics.
- Implement the issue #16 consent, opt-out, and retention decisions and define deletion/export handling before onboarding real customers.
- Use separate client-level and booking-level notes. Do not let model extraction silently create permanent notes; use owner-reviewable drafts. Do not collect or store entry/access codes in this MVP.
- Accounts are separated by environment inside one AWS Organization: a dedicated `dev` member account (`214965372605`, synthetic data only apart from the authorized testers' data in section 8.3, operator access through IAM Identity Center with MFA) and, when real clients are onboarded, a separate `pilot` account. The management account (`339713090487`) holds billing and organization administration only and runs no workload. Each environment gets its own budget and alerts.

## 8. Infrastructure and delivery

### 8.1 Infrastructure as code

- Use AWS SAM (`template.yaml`) to declare Lambda, HTTP API, DynamoDB, Cognito, SQS/DLQ, EventBridge rule, IAM policies, and log retention.
- Keep Amplify app configuration/build specification versioned; static frontend build deploys from the GitHub default branch or selected deployment branch.
- Use one AWS region, one dev stack, and one pilot/prod stack when real clients are onboarded.
- Enable DynamoDB point-in-time recovery before production use; use on-demand billing and review costs/backup policy.

### 8.2 CI/CD

- GitHub Actions runs lint/type checks, unit/integration tests, and SAM validation/build.
- GitHub Actions assumes a narrowly scoped AWS deployment role via OIDC; no static AWS access keys in repository secrets.
- Require review before deploying production stack. Separate deployment role from Lambda runtime role.
- Amplify deploys the static frontend from GitHub. Exception: the synthetic `dev` app is a manual deploy with no Git connection (owner decision, 2026-09-30, #43; see [DEV_STACK_PLAN.md](DEV_STACK_PLAN.md)), so a `customHttp.yml` change reaches `dev` only when the app's custom headers are updated too. Backend SAM deploy is triggered after tests and/or a manually approved release.
- Roll back using previous Lambda version/alias and prior frontend deployment; keep database changes backward-compatible.

### 8.3 Environments

- `local`: FastAPI + DynamoDB Local or a lightweight local adapter; mocked Twilio/OpenAI by default.
- `dev`: synthetic client data, except that once texting is enabled it may also hold authorized testers' real phone numbers, their message bodies (deleted after 90 days under the existing retention rule), and the minimal consent and opt-out evidence (kept four years after the last program text under the existing rule) (owner decision, 2026-10-01, #91), so once tester texting starts `dev` is not torn down while that evidence is retained; tester profiles use placeholder names and addresses (for example "Tester A"), so the phone number is the only real personal detail, and the owner keeps the full private consent records (name, number, time, method, the yes, and script version) himself for now, outside the repository, GitHub, and AWS; two-way SMS only with numbers onboarded in the owner app, which is the only send gate (the owner plus a few friends and family, each with documented in-person consent that also marks the profile phone verified, and a California number; numbers never go in the repo or GitHub), OpenAI plain-language texts approved with a budget limit set on the OpenAI side, the OpenAI key and Twilio auth token as Standard SecureString parameters at `/scheduling/dev/openai/api-key` and `/scheduling/dev/twilio/auth-token` (`alias/aws/ssm`), low-cost AWS stack in the dedicated member account `214965372605`; dev SMS ingress, sending, and conversations stay disabled until separately authorized.
- `pilot`: in its own future account (not yet created); actual approved Twilio number and explicitly authorized California clients; human approval always enabled; the current A2P brand is a Sole proprietor brand in the owner's individual name, so a business-registered sender is required before real-business production use (#91). Do not send live SMS until number/campaign approval, consent records, STOP/HELP handling, and owner onboarding (in-person consent and phone verification, #91) are in place.
- No need for Kubernetes, ECS/Fargate, RDS/Aurora, NAT Gateway, ElastiCache, or always-on EC2 in the MVP.

## 9. Cost strategy and tradeoffs

The target is **near-zero idle compute**, not a guaranteed zero bill. Exact monthly cost cannot be responsibly estimated until region, request/message volume, build frequency, retention, and SMS/model usage are known.

Cost controls:
- Lambda scales to zero; set memory/timeout based on observed tests and avoid provisioned concurrency.
- DynamoDB on-demand avoids provisioned read/write capacity; review table/index design to prevent expensive scans.
- Use API Gateway HTTP API rather than REST API unless a missing feature requires REST API.
- Static Amplify Hosting; avoid SSR compute and server-side rendering.
- No NAT Gateway, always-on database, container orchestration, or cache cluster.
- Bound CloudWatch log retention and redact large payloads.
- Keep SMS/model calls short and bounded; apply model token/output caps, timeout, retry limits, and a per-request budget.
- Use one or few Parameter Store secrets and avoid unnecessary scheduled invocations.
- Set AWS Budgets and billing alarms; budgets/alerts do not themselves prevent all usage.

Important cost caveats:
- SMS and LLM usage are external variable costs and likely dominate AWS costs at very low application traffic.
- Amplify build minutes/data transfer, API requests, Lambda execution, DynamoDB transactions/storage, CloudWatch retention, KMS calls, and SMS phone-number registration can be billable and depend on location/plan.
- Free-tier eligibility and service pricing change; check linked official pricing pages for the deployment region before launch.
- If Parameter Store secret rotation or advanced audit/rotation is required, prefer Secrets Manager even if its fixed per-secret cost is higher.

## 10. Alternatives considered

### RDS PostgreSQL / Aurora Serverless
Not recommended for the MVP: relational overlap constraints are attractive, but a managed database adds capacity, networking, backup, and potentially idle/scale-to-zero complexity. Revisit if calendar constraints, reporting, or multi-resource scheduling make DynamoDB transactions cumbersome.

### ECS/Fargate or EC2
Not recommended: continuous service capacity and operational overhead are unnecessary for webhook-driven low-volume traffic. Lambda is a better cost shape for intermittent use.

### DynamoDB vs PostgreSQL
DynamoDB on-demand fits scale-to-zero and the existing project precedent. Calendar overlap safety requires explicit optimistic concurrency (documented above), not naive query-then-write. PostgreSQL is the simpler relational model if the team decides correctness implementation is more valuable than minimizing idle costs.

### Lambda container image / ECR
NeuroSpineDx currently deploys the FastAPI backend as an ECR container image to Lambda. Reuse that option if shared deployment scripts, binary dependencies, or developer experience are decisive. The proposed ZIP package is the lower-complexity/low-volume default for this smaller API; both options retain Lambda's scale-to-zero billing model.

### S3 + CloudFront instead of Amplify
Potentially lower-cost for a static single-page app and gives direct control over caching/CDN, but adds hosting/deployment wiring. Amplify is selected for continuity and simpler GitHub deploys; compare actual build/traffic cost after the first pilot.

### Cognito vs custom owner login
Cognito is preferred for password and token management. Do not use SMS OTP as the owner login factor in the MVP because it adds SMS cost and operational dependency; use email/password with strong password policy and optional authenticator-app MFA. If the owner admin view is not included in the earliest POC, Cognito can be deferred rather than replaced with hand-rolled auth.

## 11. Test and operational requirements

- Unit-test availability calculation, business timezone, daylight-saving boundaries, buffer behavior, duration overrides, state transitions, hold expiry, and rescheduling.
- Concurrency test two clients requesting overlapping intervals; exactly one hold may commit.
- Test owner approval race with hold expiry and simultaneous owner edits.
- Verify SMS webhook signature validation, retry deduplication, STOP/HELP/consent paths, invalid sender handling, and delivery callback behavior.
- Test malformed or ambiguous natural-language dates and owner replies; ambiguity must not alter schedule state.
- Test model timeout/unavailability and ensure the user gets a safe fallback.
- Test outbox/SQS retry, DLQ handling, duplicate notification delivery, and replay.
- Use only synthetic data before pilot, except the authorized testers' data allowed in `dev` (section 8.3); live SMS tests may use only numbers onboarded in the owner app with recorded consent (no deploy-time recipient list); fictional 555-0100 to 555-0199 numbers are always refused by the sender.
- Track operational indicators: API errors/latency, Lambda throttles/errors, transaction conflicts, outbox age, SMS delivery failures, DLQ depth, expired holds, LLM timeout/rate, and estimated AWS spend.

## 12. Pilot decisions and remaining gates

1. Region `us-west-1` is confirmed. `dev` targets the dedicated member account `214965372605` (issue #43); `pilot` will use a separate account to be recorded when it is created. Resource provisioning and deployment need separate authorization.
2. `America/Los_Angeles` with daylight saving, Monday–Friday 8:00 a.m.–5:00 p.m., and editable observed US federal holiday closures are confirmed.
3. Twilio and California-only pilot messaging are confirmed. The in-person consent process and private yes/no evidence record are documented on the `a2p-policy-pages` branch. The owner records a clear in-person yes in the owner app (Clients tab, `POST .../clients/{client_id}/sms-consent`, which queues one welcome text, with the wording on the consent page, only for a client's first enrollment: outbox ID `welcome#<client_id>` is written in the same atomic write as the consent and phone verification, only when the phone was never verified, no such record exists, and the client has no earlier consent history record under any phone, so re-recording consent, including after STOP/START, sends nothing; the sender still enforces consent, opt-out, verified phone and the live-send gate at delivery); the full private consent record stays with the owner outside the app, and failed texts appear in the same tab (`GET .../sms-delivery-failures`). A US number is bought and the A2P 10DLC brand and campaign are approved (issue #91); STOP/HELP behavior and owner onboarding of each recipient (#91) remain before live SMS.
4. OpenAI is confirmed, and `gpt-6-luna` is the initial pilot interpretation model ID after the synthetic malformed/ambiguous-message evaluation in #24 passed twice with the final clarification prompt. The model only proposes an interpretation; the backend must validate actors, request references, dates, and permissions before any write. The owner specified no additional data-handling or budget constraints (a budget limit is now set on the OpenAI side, issue #91). No live SMS is authorized by this model selection.
5. The first POC includes the authenticated owner calendar.
6. 15-minute start increments, a current 3-hour maximum, and a 30-minute between-visit buffer with no first/last boundary buffer are confirmed.
7. One crew/resource is confirmed for the pilot.
8. The owner approved deleting SMS message bodies 90 days after the last scheduling exchange and ordinary client/appointment notes 12 months after the last visit; keep minimal consent/opt-out evidence four years after the last program text, with documented legal holds as an exception. Entry/access codes are excluded from the MVP.
9. The owner decided on 2026-10-03 that explicit client deletion overrides those retention periods and legal holds: cancel future appointments and pending requests, release reserved time, remove all client-associated records including consent and opt-out evidence, and require new onboarding and consent if the client returns. On 2026-10-04, the owner allowed only hashed markers for deleted client IDs and already-seen Twilio message IDs to reject ID reuse and delayed retries (#186, #189).

### Deletion fence for issue #189

The backend deletion transaction creates `BUSINESS#<business>/ERASURE#<sha256(client_id)>` with `state=ERASING` and the phone number only while deletion is running. It also creates a temporary `ERASURE_PHONE#<phone>` item. On completion it removes the phone item and phone attribute, leaving the client-ID hash key and `state=COMPLETE` without a TTL. The owner allows this hashed client-ID marker to reject ID reuse and prevent delayed commands from recreating data. The retained marker contains no phone number, name, message text, notes, or consent.

A client SMS sender must acquire a `CLIENT_SEND#<sha256(client_id)>` claim in a transaction that checks the erasure fence. The claim records its acquisition time. Deletion checks that no claim exists before creating the fence. The sender releases the claim only after Twilio returns and outbound evidence is written. A process crash or uncertain provider result leaves the claim in place, blocking deletion until an operator follows the [send-claim recovery procedure](PILOT_INFRASTRUCTURE.md#client-deletion-blocked-by-an-sms-send-claim); the claim has no automatic expiry because that could permit a late send after deletion. Conversation state writes check the durable client fence, and status callbacks require a live outbox item so a late callback cannot recreate evidence.

Deletion also leaves `SMS_ERASED#<sha256(provider_message_id)>` markers for inbound receipts it erased. The owner allows these hashed markers for already-seen Twilio message IDs to reject delayed retries; they contain no phone number, name, message text, notes, or consent. An old provider retry with one of those IDs is acknowledged without storing a receipt or queueing a command. Unknown senders are not persisted. For a verified client, ingress fetches the provider message's creation time through Twilio's Messages API and drops a message created before or at the latest in-person consent (or profile creation). A failed metadata fetch fails closed, allowing a provider retry without storing or queueing the message. This closes the case where an old message is first delivered after deletion and retried after fresh onboarding.

## 13. References

### Internal reference project inspected
- [EA-AI-Projects/NeuroSpineDx](https://github.com/EA-AI-Projects/NeuroSpineDx)
- Relevant files: `README.md`, `docs/NeuroSpineDx_Technical_Architecture.md`, `docs/aws-github-actions-oidc.md`, `scripts/deploy-on-demand-backend.sh`, `scripts/deploy-frontend.sh`, and `apps/backend/app/repositories/dynamodb_case_repository.py`.

### AWS official pricing references (recheck before deployment)
- [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/)
- [Amazon API Gateway pricing](https://aws.amazon.com/api-gateway/pricing/)
- [Amazon DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/)
- [AWS Amplify pricing](https://aws.amazon.com/amplify/pricing/)
- [Amazon EventBridge pricing](https://aws.amazon.com/eventbridge/pricing/)
- [Amazon Cognito pricing](https://aws.amazon.com/cognito/pricing/)
- [AWS Systems Manager Parameter Store pricing](https://aws.amazon.com/systems-manager/pricing/)
- [Amazon SQS pricing](https://aws.amazon.com/sqs/pricing/)

### External provider pricing/policy references
- [Twilio SMS pricing](https://www.twilio.com/en-us/sms/pricing)
- [OpenAI API pricing](https://openai.com/api/pricing/)
