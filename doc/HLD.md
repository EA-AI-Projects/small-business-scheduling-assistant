# High-Level Design: Smart Scheduling Assistant

**Status:** Current repository design; reviewed 2026-10-14. This describes implemented capabilities, not live deployment state.

## 1. Design goals

Provide one schedule for SMS, the owner app, and the client portal. The model interprets language and explains results; scheduling services enforce identity, availability, approval, and state transitions. The initial business has one schedulable crew and one service.

## 2. System overview

```text
Client / owner SMS -> Twilio -> signed webhook -> receipt queue -> conversation worker
                                                                       |
                                                             bounded OpenAI tools
                                                                       |
Owner / client browser -> static Next.js app -> authenticated API -> domain services
                                                                       |
                                                                   DynamoDB
                                                                       |
                                                       transactional notification outbox
                                                                       |
                                                       dispatcher -> SQS -> SMS sender -> Twilio

Scheduled workers: hold expiry, note retention, SMS retention, booking invitations
```

The calendar and stored appointment state are authoritative. Queue messages identify work; they do not authorize an action. Each worker rereads current state. SMS and browser actions use the same scheduling services.

## 3. Components and responsibilities

| Component | Responsibility |
| --- | --- |
| SMS ingress | Validate provider signatures and exact callback URLs, resolve verified senders, persist and deduplicate receipts, record keyword and delivery events. |
| Conversation service | Interpret messages using bounded sender-scoped history and typed tools; retain short-lived offers and confirmation questions. See [CONVERSATION.md](CONVERSATION.md). |
| Scheduling domain | Calculate availability, create holds, approve/decline, cancel, swap replacements, edit appointments/blocks/policy, and commit audit and notification records. |
| Owner app | Calendar, pending decisions, clients, notes, consent, delivery failures, and business/policy/invitation settings. |
| Client portal | Calendar and Appointments pages for the linked client's own visits, open times, requests, cancellation, withdrawal, and moves. |
| Identity service | Link approved owners to businesses and invited clients to existing profiles. Cognito sign-in alone grants no data access. |
| Background workers | Expire holds, dispatch/deliver notifications, purge retained content, and select/promote eligible invitations. |

## 4. Logical data model

- **Business policy and profile:** timezone, hours, holidays/exceptions, horizon, limits, buffer, hold lifetime, business/support details, and one service with a default duration.
- **Client:** owner-maintained identity/contact/address, active state, phone verification and separate consent/opt-out evidence. No per-client visit duration.
- **Account link:** verified subject, role, business, optional client, activation state, version and administrative audit.
- **Appointment:** client, UTC interval, duration/buffer snapshots, status/version, hold expiry, and optional original appointment being replaced.
- **Unavailable block:** owner-managed interval and version.
- **Note:** ordinary client or appointment note with retention and documented legal hold; entry/access codes are excluded.
- **Conversation:** durable inbound receipts, accepted outbound history, and sender-bound expiring prompt state.
- **Audit and outbox:** committed action evidence and stable notification intents with independent delivery state.
- **Outreach:** versioned invitation settings, selected intents, and scheduled repeat guards.

Storage keys and concurrency rules belong in [ARCHITECTURE.md](ARCHITECTURE.md).

## 5. Booking state machine

```text
PENDING_APPROVAL -> CONFIRMED  (owner approval)
                 -> DECLINED  (owner decline)
                 -> EXPIRED   (hold deadline)
                 -> CANCELLED (client withdrawal)
CONFIRMED        -> CANCELLED (authorized cancellation or approved replacement)
```

An unexpired pending request reserves time. Confirmed appointments reserve their interval and the applicable inter-visit buffer. Terminal states release time. Reads ignore expired holds even before the expiry worker runs.

A move creates a pending replacement and keeps the original confirmed. Approval swaps both in one transaction; decline, withdrawal, expiry, or a failed approval leaves the original unchanged. An accepted owner counteroffer also creates a pending request; approval resolves its original pending request in the same transaction. See [SCHEDULING_CONTRACTS.md](SCHEDULING_CONTRACTS.md).

## 6. Availability and conflict handling

The saved policy controls working hours, exceptions, holidays, horizon, start increments, and duration/buffer caps. The saved business profile supplies the service duration; an owner may override a particular appointment. Existing appointments retain their duration and buffer snapshots.

The approved baseline is `America/Los_Angeles`, weekdays 08:00–17:00, observed US federal holidays closed, a 14-day client booking horizon, 15-minute starts, a 24-hour pending hold, a 3-hour maximum visit, and a configurable 30-minute buffer between visits. There is no buffer at the opening/closing boundary or around blocks. Settings can change the policy subject to validation against existing reservations.

Availability excludes confirmed visits, unexpired holds, and authoritative unavailable blocks. Every calendar write revalidates against current state and conditionally advances the business calendar revision in its atomic transaction. UTC storage and business-local rendering keep daylight-saving handling explicit.

## 7. Key workflows

- **Booking:** identify an eligible sender, offer current times, receive a later acceptance of one stored option, then create a pending hold and owner/client notices. The portal uses an explicit send action with the same hold service.
- **Approval:** the owner identifies one versioned pending request; the service rechecks expiry and conflicts, commits the decision, and queues notices.
- **Cancellation:** SMS prepares a question bound to one appointment/version and requires a later confirmation. The portal requires a separate confirmation press. A pending replacement must be resolved before cancelling its original.
- **Rescheduling:** offer replacement times for one current confirmed visit, create a pending replacement, and await owner approval.
- **Invitations:** an owner-configured schedule or confirmed manual run selects eligible clients. Send-time checks can suppress queued invitations; an invitation creates no reservation. Delivery requires separate gates.

## 8. AI boundaries and safeguards

Models receive typed, scoped tool interfaces and bounded conversation context. They cannot choose identity, notification destinations, arbitrary database writes, or override scheduling rules. Client writes require stored offers or confirmation state; owner writes require current request/version checks. Duplicate receipts replay stored results.

The backend validates explicit dates, times, references, and message size where applicable. It does not prove every prose claim or status association; model wording and option presentation retain an accepted risk. Deterministic fallback text remains available. [CONVERSATION.md](CONVERSATION.md) owns the precise model/fallback boundaries.

## 9. Privacy and reliability

Client reads expose only that client's records. Web sign-in, phone verification, in-person SMS consent, and opt-out are separate gates. Secrets stay in managed storage; logs and audit events avoid message bodies and contact details.

Ordinary notes expire 12 months after the last completed visit (or creation when there is no completed visit), subject to documented legal holds. SMS bodies expire 90 days after the client's last scheduling exchange; the owner's texts expire individually after 90 days. Minimal consent/opt-out evidence has a four-year clock; an active suppression marker survives evidence expiry. Client erasure fences concurrent work and leaves only the approved hashed replay/reuse markers. See [ARCHITECTURE.md](ARCHITECTURE.md#7-privacy-retention-and-erasure).

Appointment commits include outbox records. Failed SMS delivery does not undo a booking. Worker retries are idempotent, while uncertain provider acceptance can still produce duplicate ordinary notifications; invitation uncertainty is held for reconciliation.

## 10. Deployment and operations

AWS SAM defines the serverless backend in `us-west-1`; Amplify serves the static app. Dev uses a dedicated account and manual deployments. A pilot account and real-data launch require their own operational review. Live deployment state is read from AWS and operator records, not inferred from this document. See [PILOT_INFRASTRUCTURE.md](PILOT_INFRASTRUCTURE.md).

## 11. Testing strategy

Retained checks cover stateful scheduling workflows and selected API/provider/queue/storage boundaries with synthetic data. DynamoDB Local checks exercise conditional transaction races. CI also checks backend/frontend types, lint, generated contracts, infrastructure, scripts, and static exports. Scripted models prove backend boundaries; model evaluations measure language behavior separately. Neither establishes a complete deployed end-to-end proof. See [AWS_VALIDATION.md](AWS_VALIDATION.md) and [MODEL_EVAL.md](MODEL_EVAL.md).

## 12. Deferred scope

Recurring appointments, multiple crews/services, dynamic route planning, payments, native apps, and protected entry-code storage require separate design. Cancellation-triggered outreach is not implemented. Current requirements live in [PRD.md](PRD.md); prior choices live in [DECISION_HISTORY.md](DECISION_HISTORY.md).
