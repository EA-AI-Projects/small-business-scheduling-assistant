# Technical Architecture: Small Business Scheduling Assistant

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
| Backend API | Python 3.13 + FastAPI + Mangum, one AWS Lambda function (ZIP package) | Matches NeuroSpineDx backend language/framework; scales to zero and is inexpensive at pilot traffic |
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
| AWS region | Configure one region close to the business and SMS provider; confirm before deployment | No region has been selected yet; avoid hard-coding the NeuroSpineDx region without checking latency, service availability, and SMS registration |

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

- Twilio (or another selected SMS provider) for the SMS phone number, inbound SMS, outbound messages, and delivery status.
- OpenAI API (or a selected alternative) for message interpretation and response drafting.

The core calendar, appointment state, approval policy, and availability calculation stay in AWS and do not depend on either external provider being available.

## 4. Application components

### 4.1 Owner web application

- Next.js, React, TypeScript; mobile-first because the owner may mostly use a phone.
- Build with static export and host via Amplify Hosting. No Next.js SSR or server actions in the initial design; all data operations use the API.
- Cognito signs in the owner. API Gateway validates the owner access token for admin routes.
- Screens: day/week schedule, pending approvals, client list/profile, unavailable blocks, and a small settings screen.
- Client-facing booking portal is not part of the MVP; the client workflow is SMS.

### 4.2 API/backend

- FastAPI app served through Mangum from AWS Lambda.
- One backend codebase; route groups have distinct authorization. A single Lambda deployment is sufficient initially. A separate worker Lambda may use the same package only if background responsibilities justify it.
- Route categories:
  - `POST /webhooks/sms/inbound` — provider-signed inbound messages.
  - `POST /webhooks/sms/status` — delivery status callbacks.
  - `/owner/*` — authenticated owner schedule, client, configuration, and pending-request operations.
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
- Calendar records have sort keys beginning `EVENT#<UTC-start>#<event_id>`; query the business partition by a bounded time range and use strongly consistent base-table reads for authoritative availability.
- Appointment metadata may be stored at `PK = APPOINTMENT#<appointment_id>, SK = META` and written transactionally with its calendar event record.
- Client records use `PK = BUSINESS#<business_id>, SK = CLIENT#<client_id>`; client-phone lookup can use a GSI or a dedicated phone-index item. Treat GSI reads as non-authoritative for writes because GSIs are eventually consistent.
- Store a per-business calendar revision item: `PK = BUSINESS#<business_id>, SK = CALENDAR#REVISION`.
- Keep separate records for appointments, unavailable blocks, client-level notes, appointment-level notes, conversation state, outbox events, and audit events. Avoid storing sensitive access codes in general note fields.

Partitioning by business prepares the data model for additional businesses without introducing a multi-region or sharded system. The initial deployment is still one business and one schedulable crew/resource.

### 5.2 No-double-booking strategy

DynamoDB does not provide SQL range-exclusion constraints. Do not implement availability as a read-then-write sequence with no concurrency protection.

Recommended low-volume single-business strategy:
1. Strongly read the current business calendar revision and all relevant calendar events for the candidate interval, including any maximum visit duration/buffer lookback needed to catch an event already in progress.
2. Calculate availability with deterministic code.
3. Use one `TransactWriteItems` operation to conditionally advance the calendar revision from the value read, write the pending hold/appointment metadata and calendar event, and write an idempotency record.
4. If the revision condition fails, re-read availability and retry a bounded number of times. If the slot is no longer available, offer alternatives.
5. Apply the same conditional transaction discipline to approvals, cancellations, owner blocks, and reschedules.

This serializes competing changes to a small business calendar through optimistic concurrency without running a lock server. Keep transactions small and test overlapping requests under concurrency. If volume or multi-crew scheduling grows, revisit resource partitioning and a relational database with exclusion constraints.

### 5.3 Time, duration, and buffer

- Store instants as UTC epoch/time values and store the business timezone as an IANA timezone identifier.
- Interpret client local-date phrases in the business timezone; test daylight-saving transitions.
- Home-size category maps to an owner-configured estimated duration. Snapshot duration on each appointment; allow an owner override even after approval.
- Appointment slots must fit the entire duration within business hours and must not overlap confirmed events, active pending holds, or unavailable blocks.
- Default fixed travel buffer is 30 minutes. The implementation must settle whether buffer is modeled after each visit or between neighboring visits; the architecture supports a fixed configurable value, not route-aware estimation.
- No dynamic address-based routing in MVP.

### 5.4 Holds and expiry

- Pending request stores `hold_expires_at` in UTC. Availability code treats a hold as inactive as soon as `hold_expires_at <= now`, regardless of whether cleanup has run.
- EventBridge invokes a lightweight expiry job periodically (e.g. every 5–15 minutes, exact cadence TBD) to transition expired holds and enqueue client notices.
- The expiry job is idempotent. Before expiry, an owner approval checks the timestamp transactionally; a late approval cannot confirm an expired request.
- DynamoDB TTL can be used only to clean up disposable records after their retention period; TTL is asynchronous and must never be relied on for availability or exact expiration timing.

## 6. Notifications and reliability

- For every committed state change, transactionally write an outbox record with the change. A worker sends client/owner SMS asynchronously through SQS-triggered Lambda.
- Configure SQS redrive to a DLQ, bounded retries, and CloudWatch alarms for DLQ depth and age of oldest message.
- Sending is at-least-once. Include idempotency keys/provider IDs and design templates so retries do not cause duplicate bookings or contradictory state; provider send idempotency may be limited, so log delivery attempts.
- Persist booking state before telling either party that it changed.
- SMS delivery failure does not roll back the appointment. It creates a visible notification failure for owner follow-up.
- Webhook handler acknowledges only after durable message/event persistence. Deduplicate by provider event ID.

## 7. Security and privacy

- Public internet API, no Lambda VPC for MVP. Lambda uses IAM-authorized AWS service endpoints and outbound HTTPS to Twilio/OpenAI. Avoiding a NAT Gateway removes a potentially disproportionate always-on network cost. Reassess VPC only for a concrete private-resource requirement.
- API Gateway Cognito authorizer on owner routes. Restrict inbound SMS endpoints to valid provider signatures; use rate limits/WAF only if abuse or traffic warrants its cost.
- Owner SMS approvals are allowlisted by phone and resolved against pending request state; ambiguous approvals are rejected pending clarification.
- Least-privilege Lambda execution role scoped to required DynamoDB keys/table, SQS queue, Parameter Store names/KMS key, and CloudWatch logs.
- Store third-party keys as SecureString parameters; retrieve/cache at runtime. Never place keys in source, frontend bundles, model prompts, or logs.
- Do not log raw SMS body, access code, full address, or model prompt by default. Use message/request IDs and redacted structured metadata for diagnostics.
- Define client consent, message opt-out, record retention, and deletion/export policies before onboarding real customers.
- Use separate client-level and booking-level notes. Do not let model extraction silently create permanent notes; use owner-reviewable drafts. Exclude entry codes from ordinary notes; decide on a separate protected design if the business insists on storing them.
- Single AWS account is acceptable for a pilot only with separate dev/prod naming, restricted IAM, budgets/alerts, and no real customer data in development.

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
- Amplify deploys the static frontend from GitHub. Backend SAM deploy is triggered after tests and/or a manually approved release.
- Roll back using previous Lambda version/alias and prior frontend deployment; keep database changes backward-compatible.

### 8.3 Environments

- `local`: FastAPI + DynamoDB Local or a lightweight local adapter; mocked Twilio/OpenAI by default.
- `dev`: synthetic client data, SMS sandbox/test number where available, low-cost AWS stack.
- `pilot`: actual business number and explicitly authorized clients; human approval always enabled.
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
- Use only synthetic data before pilot; live SMS tests must use explicitly authorized test numbers.
- Track operational indicators: API errors/latency, Lambda throttles/errors, transaction conflicts, outbox age, SMS delivery failures, DLQ depth, expired holds, LLM timeout/rate, and estimated AWS spend.

## 12. Deployment decisions to confirm

1. AWS region and whether there is an existing project AWS account to use.
2. Actual operating days and timezone.
3. Twilio vs another SMS provider, business jurisdiction, phone-number type, registration and consent requirements.
4. OpenAI vs another model provider and a small tool-call evaluation before choosing a model ID.
5. Whether the initial owner calendar/admin view is required for POC or can arrive with MVP.
6. Schedule granularity (recommend 15-minute increments) and maximum visit duration/buffer semantics.
7. Whether one crew/resource is a safe initial assumption.
8. Message and note retention duration; access-code storage remains excluded absent a separate security decision.

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
