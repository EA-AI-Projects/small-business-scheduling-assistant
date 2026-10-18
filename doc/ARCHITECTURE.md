# Technical Architecture: Smart Scheduling Assistant

**Status:** Current repository architecture; reviewed 2026-10-14. Sources: `template.yaml`, `backend/scheduling/`, `frontend/`, `infra/`, and `scripts/dev/`. This is not a live AWS inventory.

## 1. Purpose

Describe the runtime, module boundaries, storage, authorization, and correctness rules. Product requirements belong in [PRD.md](PRD.md), system workflows in [HLD.md](HLD.md), SMS details in [CONVERSATION.md](CONVERSATION.md), and deployment procedures in [PILOT_INFRASTRUCTURE.md](PILOT_INFRASTRUCTURE.md). Historical rationale is separate in [DECISION_HISTORY.md](DECISION_HISTORY.md).

## 2. Stack at a glance

| Concern | Implementation |
| --- | --- |
| Web app | Next.js Pages Router, React, TypeScript, static export; Amplify Hosting `WEB`. |
| Authenticated API | Python 3.12, FastAPI, Mangum, ZIP-packaged Lambda; API Gateway HTTP API. |
| Web identity | Cognito user pool, authorization code with PKCE, active application identity links. |
| Scheduling storage | Single on-demand DynamoDB table, strongly consistent authoritative reads, conditional transactions. |
| Work delivery | SQS Standard outbox and conversation queues, each with a DLQ. |
| Background work | Lambda functions invoked by EventBridge rules or SQS mappings. |
| SMS / model | Twilio and OpenAI behind adapters. |
| Credentials | Environment-scoped SSM Standard SecureString paths; narrow runtime reads. |
| Operations | CloudWatch logs, metrics and alarms; SNS alarm email subscription. |
| Infrastructure / delivery | SAM backend template, separate deployment-role template, manual dev scripts; GitHub Actions runs CI. No GitHub OIDC deployment workflow is implemented. |
| Isolation | `us-west-1`; dedicated dev account, separate pilot account planned. No account IDs or live resource IDs belong in reference docs. |

## 3. Runtime topology

```text
Browser -> Amplify static app -> Cognito sign-in
                           -> HTTP API JWT authorizer -> owner/client API Lambda -> DynamoDB

Twilio -> signed SMS HTTP API -> ingress Lambda -> DynamoDB receipts
                                                -> conversation SQS -> conversation Lambda
                                                                           -> OpenAI
                                                                           -> domain + DynamoDB

EventBridge -> expiry / retention / invitation workers -> DynamoDB
EventBridge -> outbox dispatcher -> outbox SQS -> sender Lambda -> Twilio
Twilio delivery callbacks -> signed ingress -> outbox-linked delivery status
```

The web API and SMS ingress are separate Lambda/API boundaries. SMS ingress is conditional. Conversation processing and sending require independent authorization parameters. Functions run without a VPC; the design has no NAT gateway, application server, relational database, or model server.

## 4. Application components

### 4.1 Web app

`frontend/` statically exports owner and client pages. Owner screens cover calendar, requests, clients, notes/consent, delivery failures, and Settings. Settings presents four saved-value cards with one focused editor at a time; [OWNER_SETTINGS_UI_SPEC.md](OWNER_SETTINGS_UI_SPEC.md) owns interaction details.

Client Calendar and Appointments share neutral calendar components with the owner UI. They render only the linked client's upcoming confirmed visits and live pending requests. Day/Week views place bookable starts in the grid; Month/Schedule/Year show visits. Client navigation is bounded by the booking horizon, with a Day explanation when a tapped date lies beyond it. Appointment and move actions use the existing versioned client API. A held move never falls back to a new-booking write when its original becomes stale.

The browser keeps access tokens in page memory. The ID-token email is display-only and does not authorize API access. Public build variables identify API/Cognito origins and app configuration, never secrets. The build inserts a restrictive CSP; `customHttp.yml` supplies hosting headers. CORS permits only the configured exact app origin, methods and headers, without credentials. API Gateway answers preflight without JWT authorization.

### 4.2 HTTP and identity boundaries

`owner_lambda.py` constructs `create_cognito_linked_app` from `linked_auth.py`. The API verifies signature, issuer, expiry, app client and access-token type, then resolves the active application link. API Gateway also uses a JWT authorizer. Owner routes under `/v1/owner/businesses/{business_id}` require an owner link for that business; `/v1/client/` routes derive business/client from the link. Browser-supplied business or role never grants access. The `OwnerSub` template parameter remains a bootstrap configuration value; current linked authorization derives owner access from the stored link.

Owner API schemas are generated into `frontend/openapi/owner.json` and `frontend/src/api/schema.d.ts`. Client routes expose session, availability, bookings, requests, cancellation and rescheduling. Internal contracts and error mapping are in [SCHEDULING_CONTRACTS.md](SCHEDULING_CONTRACTS.md).

An owner is administratively approved and linked. A client invitation uses the optional email saved in an existing profile, creates a pending link, and expires after 24 hours. The invitation API requires the expected profile version and resolves its saved email on the server. Contact email edits preserve pending invitations and active links; replacing account access requires explicit revocation and a new invitation (#375). Activation validates the Cognito ID token, invited verified email, subject, profile and expiry. Email recovery restores sign-in, not record scope. Revocation removes access; a subject cannot be silently retargeted. Provisioning details are in the [README](../README.md#account-provisioning-and-recovery).

Identity storage uses `ACCOUNT#sha256(sub) / LINK`, a business-scoped `ACCOUNT_TARGET#...` pointer and numbered audit events. Link and target updates are conditional and atomic. Resolution checks both and the referenced policy/profile, including the client deletion fence. Erasure removes the client's link, target and link audit.

### 4.3 Domain and adapters

`backend/scheduling/domain/` owns availability, holds, lifecycle, policy, appointments/blocks, client records, profile/duration, conversation, counteroffers, invitations, outbox, expiry and retention behavior. DynamoDB, Twilio, SQS, Cognito and OpenAI adapters live under `adapters/`. `workers/` wires durable work to those services. Local entry points use synthetic in-memory repositories and are not deployed routes.

Conversation tools derive identity from the verified receipt, validate strict arguments, and call existing domain services. Model failure never authorizes a write; a committed action is not retried just because reply generation fails. [CONVERSATION.md](CONVERSATION.md) defines transcript, tool-loop, stored-prompt and final-text rules.

## 5. Data architecture and conflict correctness

### 5.1 DynamoDB records and access patterns

| Record | Key/access pattern |
| --- | --- |
| Business policy/profile | `BUSINESS#<business_id>` with `POLICY#SCHEDULING` and `PROFILE#BUSINESS`; versioned configuration. |
| Calendar/revision | Business `EVENT#<UTC-start>#<event_id>` projections and `CALENDAR#REVISION`. |
| Appointment | `APPOINTMENT#<id> / META`, written with its calendar event. |
| Blocks | Business `BLOCK#<id>` authoritative intervals; calendar events are UI projections. |
| Clients and notes | Business/client-scoped records; dedicated verified-phone lookup and separate consent/opt-out records. |
| Completed visit lookup | `VISITS#<business_id>#<sha256(client_id)> / END#<UTC-end>#<id>`; strongly consistent reverse query for retention. |
| Identity | Hashed subject link, business target pointer and administrative events. |
| Conversation/history | Receipt and accepted-send records, actor-scoped `SMS_HISTORY#` pointers and short-lived conversation state. |
| Invitations | `SETTINGS#BOOKING_OUTREACH`, deterministic invitation intents, scheduled per-client guard. |
| Due work | Sparse `HoldDueIndex` and `OutboxDueIndex`; wake-up indexes only, followed by authoritative rereads. |

The table has PITR, `expires_at_epoch` TTL and retain policies on deletion/replacement. TTL cleans up eligible storage asynchronously; it does not define hold validity, consent, or send eligibility.

The business profile is one versioned service record: display name, description, support contact, service name/description and default duration. Profile saves and policy saves guard each other's validated versions so the default cannot exceed the policy maximum. Default-duration hold commits also guard the quoted profile duration. Existing appointments keep snapshots. Exact field limits are declared by `BusinessProfileBody` in `owner_api.py` and its generated schema.

### 5.2 No-double-booking transaction

1. Strongly read the calendar revision **before** calendar, policy and block reads.
2. Strongly read policy and fully paginate relevant events. The visit window is bounded by configured maximum duration and buffer, not by a result-count limit. Query all authoritative `BLOCK#` records separately so an arbitrarily long block is not lost to the visit lookback.
3. Check working windows, exceptions, holidays, duration/caps, horizon where applicable, active reservations and buffers. Ignore holds with `hold_expires_at <= now`.
4. Commit appointment/block/policy changes, projections, conditional revision advance, idempotency result, audit and required outbox records in one DynamoDB transaction. Reject changes that cannot fit atomically.
5. On revision conflict, retry from the first read within a bounded budget; return current conflict information if the slot is no longer valid. A reused key with different content is rejected; an identical replay returns the committed result.

All calendar writes follow this discipline, including holds, approvals, declines, expiry, cancellation, owner edits, blocks, policy edits and replacement swaps. A replacement can overlap only its own original; approval validates every other reservation and atomically resolves both. An active replacement prevents independent approval/cancellation of the original where that would orphan the swap.

Approval checks status, expected version and a fresh `decision_at` immediately before transaction submission, requiring expiry strictly later than that time. The expiry worker uses the opposite condition; neither can overwrite the other's committed transition.

### 5.3 Time, duration and buffer

Store timezone-aware UTC instants and resolve owner local inputs in the saved IANA business zone; reject ambiguous and nonexistent wall-clock times. The saved profile supplies one default service duration, with explicit owner overrides. There are no home-size categories or per-client default durations.

Visits fit wholly inside working windows. The gap between visits is at least `max(candidate.buffer_minutes, existing.buffer_minutes)`; no opening/closing or block-edge buffer is added. Policy changes validate future reservations and reject caps below active snapshots or the profile default. Baseline values are in [HLD.md](HLD.md#6-availability-and-conflict-handling).

## 6. Work queues and notifications

Scheduling commits store immutable, stable-ID notification intents with the state change. The dispatcher queries due `PENDING`, `RETRYABLE` and lease-expired `SENDING` records, rereads them, sends identifiers to SQS, and conditionally advances dispatch timing. Queue duplication is expected.

The sender conditionally claims due work with a lease/attempt, checks current recipient and event state, then records provider acceptance or bounded retry/backoff. Partial-batch failures preserve successful queue work. Exhausted attempts become visible failures; delivery callbacks do not roll back appointments. Ordinary provider timeouts after acceptance may cause a duplicate on retry.

Scheduled invitations use versioned owner settings, current consent/phone/opt-out/appointment checks, repeat history and a transactional selection guard. Manual invitations ignore the automatic enabled flag and repeat interval, use a retryable request key/cursor, and require a separate delivery gate plus retention. Settings edits suppress queued scheduled selections. Invitation acceptance uncertainty keeps a held claim for operator reconciliation instead of automatic retry. [CONVERSATION.md](CONVERSATION.md#scheduled-booking-invitations-249) owns eligibility and timing details.

## 7. Privacy, retention and erasure

Provider signatures protect SMS ingress; verified sender/profile, consent, opt-out and live-delivery gates are rechecked when work runs and when it sends. A phone change clears verification. In-person consent capture atomically verifies the recorded phone; email account activation does neither. Runtime secrets come from SecureString paths and do not enter model prompts, app bundles or logs.

Ordinary notes exclude recognized access-code phrases. A documented legal hold prevents purge. Completed-visit retention reads the strongly consistent `VISITS#` collection and conditionally deletes against the calendar revision, avoiding stale purge decisions. Notes expire 12 months after the last completed visit, or creation if none exists.

Client SMS bodies expire 90 days after the last scheduling exchange; owner texts expire 90 days per message. Consent and immutable STOP evidence expire four calendar years after the last provider-verified inbound or accepted outbound program text (agreement date if none). An active phone suppression marker persists until fresh consent followed by START clears it. Evidence expiry alone cannot opt a sender back in. A trusted administrative method supports documented SMS evidence legal holds; an operational management path must be reviewed before real-data onboarding.

Client erasure first creates an `ERASURE#sha256(client_id)` fence and temporary phone fence. Senders acquire a `CLIENT_SEND#sha256(client_id)` claim only when no erasure fence exists; deletion refuses while a claim exists. An uncertain/crashed send leaves a claim with no automatic expiry. Recovery is in [PILOT_INFRASTRUCTURE.md](PILOT_INFRASTRUCTURE.md#client-deletion-blocked-by-an-sms-send-claim).

Explicit client erasure overrides ordinary retention and documented legal holds. It does not redact owner-thread texts naming the client; those retain their individual 90-day expiry. Completed erasure removes the client-associated personal records and the temporary phone fence, retaining only the approved hashed client-ID reuse marker and `SMS_ERASED#sha256(provider_message_id)` retry markers. Conversation writes check the fence; delivery callbacks require a live outbox record. Ingress checks provider creation time for verified clients and rejects messages predating current consent/profile creation; failed metadata checks fail closed.

## 8. Infrastructure and delivery

`template.yaml` owns backend resources; `infra/dev-deploy-roles.yaml` owns separate scoped operator/execution roles and the runtime permissions boundary. Amplify, artifact storage, budgets and SecureString values have separate setup steps. The runtime stack does not provision their secret values.

GitHub Actions validates code, generated contracts, templates, scripts and static export. Dev scripts deploy the local checkout through reviewed change sets and manual Amplify uploads. They guard target identity/region, dirty state and trigger changes. There is no automatic backend deploy or implemented OIDC deploy role. Pilot deployment needs its own reviewed configuration and role/runbook boundary.

EventBridge rules: hold expiry every five minutes, outbox and invitation selection every minute, note retention daily at 09:00 UTC and SMS retention daily at 10:00 UTC. Schedules, sender mapping and delivery gates default disabled; defaults do not describe current live settings. Lambda log groups retain 30 days. Error/DLQ/oldest-due alarms publish to a confirmed SNS email subscription.

For setup, IAM limitations, enablement, recovery and deployed proof, see [PILOT_INFRASTRUCTURE.md](PILOT_INFRASTRUCTURE.md), [AWS_DEPLOYMENT_ROLES.md](AWS_DEPLOYMENT_ROLES.md), [DEV_STACK_PLAN.md](DEV_STACK_PLAN.md), and [AWS_VALIDATION.md](AWS_VALIDATION.md).

## 9. Limits and deferred alternatives

One business calendar revision intentionally serializes low-volume writes. Multi-crew scheduling or higher contention requires revisiting resource partitioning or a relational database with exclusion constraints. Recurrence, routing, payments and multi-service catalogs are outside the current design. Model wording is not fully semantically validated, and component tests do not establish deployed end-to-end behavior.

Current costs depend on actual configuration and provider usage. Refresh estimates before spending; dated estimates and deployment checkpoints are historical evidence, not architecture requirements or standing authorization.
