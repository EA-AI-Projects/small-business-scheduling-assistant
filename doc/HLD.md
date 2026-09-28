# High-Level Design: Small Business Scheduling Assistant

**Status:** Draft for review  
**Version:** 0.1  
**Date:** 2026-09-25  
**Related:** [PRD.md](PRD.md)

## 1. Design goals

- One authoritative schedule shared by SMS conversations and the owner interface.
- Keep language-model behavior separate from authoritative booking decisions.
- Prevent overlapping confirmed visits, pending holds, and unavailable blocks.
- Make owner approval easy from SMS while preserving an audit trail.
- Minimize sensitive data collection and protect client information.
- Keep the first implementation small enough for a single-business pilot.

## 2. Proposed architecture

```text
 Client SMS                         Owner SMS
     |                                  ^
     v                                  |
+----------------+                +------------------+
| SMS Provider   |<-------------->| Notification /   |
| inbound webhook|                | SMS adapter       |
+-------+--------+                +---------+--------+
        |                                   ^
        v                                   |
+-----------------------+                   |
| Webhook/API service   |-------------------+
| auth, validation,     |
| idempotency, routing  |
+-----------+-----------+
            |
     +------+-----------------------+
     |                              |
     v                              v
+----------------------+   +-----------------------+
| Conversation service |   | Scheduling domain     |
| intent/slot gathering|-->| availability, holds,  |
| LLM with bounded     |   | approvals, cancel,    |
| tools and policies   |   | duration, audit       |
+----------------------+   +-----------+-----------+
                                     |
                                     v
                              +-------------+
                              | DynamoDB    |
                              | on-demand   |
                              +------+------+ 
                                     ^
                                     |
                         +-----------+-----------+
                         | Owner web admin       |
                         | calendar/configuration|
                         +-----------------------+
```

The SMS provider is an external dependency. The LLM is called only from the conversation service. The scheduling domain exposes explicit operations; the model cannot directly write database records or calculate authoritative availability by free-form reasoning.

## 3. Components and responsibilities

### 3.1 SMS adapter and webhook/API service

- Receive provider webhooks for inbound/outbound message events.
- Verify provider signatures and reject invalid requests.
- Normalize phone numbers and message metadata.
- Deduplicate provider retries using provider message IDs/idempotency keys.
- Persist inbound messages before processing so transient downstream failures can be retried.
- Rate-limit and protect endpoints; avoid logging message bodies or phone numbers unnecessarily.

### 3.2 Conversation service

- Associate inbound phone numbers with a client or an unrecognized-contact flow.
- Load limited relevant context: client identity, current appointment/request state, and approved notes needed for the conversation.
- Use the LLM to classify/interpret the message and formulate a response.
- Invoke only typed, allowlisted tools, such as `get_available_slots`, `create_booking_request`, `cancel_appointment`, `get_client_appointments`, and `create_note_draft`.
- Require confirmation/clarification when identity, date, time, appointment, or owner intent is ambiguous.
- Never tell a client that a booking is confirmed unless the scheduling service returns a persisted confirmed state.
- Keep model prompts and response generation free of secrets and unnecessary personal data.

### 3.3 Scheduling domain service

Owns all business rules and state transitions:
- Working-hours and booking-horizon enforcement.
- Duration selection from client defaults and appointment overrides.
- Buffer application.
- Availability queries.
- Pending request creation and hold expiration.
- Owner approval/decline.
- Cancellation and schedule changes.
- Conflict detection and transactional persistence.
- Audit events and notification outbox creation.

### 3.4 Owner notification and reply handling

- Send concise SMS notifications for pending requests and cancellations.
- Include a unique, non-guessable or otherwise securely scoped request reference when needed to disambiguate concurrent requests.
- Resolve owner replies against a pending action. If more than one action matches, ask the owner to select/clarify; do not mutate state.
- Natural-language parsing can identify intent, but the scheduling service validates that the requested transition is allowed.
- Send owner a result receipt after the operation.

### 3.5 Owner web admin

- Authenticated, mobile-friendly minimal interface for calendar/list review, pending requests, clients, unavailable blocks, and configuration.
- Role/access controls even if the first deployment has one owner account.
- Every write action uses the same scheduling domain service as SMS.
- Basic audit history for changes, including actor/source and timestamps.

### 3.6 Database and background jobs

- DynamoDB on-demand stores clients, appointments, blocks, conversations, notes, configuration, audit events, and notification outbox records.
- Scheduled Lambda jobs expire holds, recover due outbox records, and purge ordinary notes after the approved retention period; SQS/Lambda handles outbound delivery.
- Use transactional outbox pattern so state changes and required notifications are not separated by a crash.
- Use a strongly consistent calendar read and conditional calendar-revision update in one DynamoDB transaction to prevent overlapping active reservations for the single crew.

## 4. Core data model (logical)

### Business
- `id`, `name`, `timezone`, `working_hours`, `booking_horizon_days`, `default_buffer_minutes`, `default_hold_minutes`, `owner_phone`

### Client
- `id`, `name`, `phone_e164` (unique per business unless shared-number handling is added), `service_address`, `home_size_category`, `default_duration_minutes`, `active`, timestamps

### Appointment
- `id`, `business_id`, `client_id`, `start_at`, `end_at`, `duration_minutes`, `buffer_minutes`, `status`, `requested_by`, `approved_by`, `hold_expires_at`, optional `replaces_appointment_id`, timestamps
- Preserve the appointment’s duration and buffer snapshot so later profile/config changes do not silently rewrite existing bookings.

### UnavailableBlock
- `id`, `business_id`, `start_at`, `end_at`, `reason` (optional), `created_by`, timestamps

### Note
- `id`, `business_id`, `client_id`, nullable `appointment_id`, `body`, `created_by`, `source`, `review_status`, timestamps
- Client-level note has no appointment ID; visit-specific note references an appointment. Exclude entry/access codes entirely from the MVP.
- Delete ordinary client and appointment notes 12 months after the last completed visit, except during a documented legal hold. For a client with no completed visit, delete each note 12 months after its creation. Reject a new note if the last completed visit was already more than 12 months ago.

### Conversation and Message
- `Conversation`: client/phone association, current conversation state, timestamps.
- `Message`: provider message ID, direction, delivery status, minimized/redacted content where possible, timestamps.
- Delete SMS message bodies 90 days after the last scheduling exchange. Keep minimal consent and opt-out evidence separately for four years after the last program text, except during a documented legal hold. Do not copy full message bodies into that evidence record.

### AuditEvent
- Actor (owner/client/system), source, action, entity reference, timestamp, and minimal before/after fields needed for accountability. Avoid copying sensitive message content into audit records.

### OutboxMessage
- Destination reference, template/type, payload reference, retry state, provider ID, timestamps.
- Protect phone numbers/message content and apply retention limits.

## 5. Booking state machine

```text
Client request
     |
     v
PENDING_APPROVAL --owner approves--> CONFIRMED
       |                                  |
       | owner declines                   | client cancels
       v                                  v
   DECLINED                           CANCELLED
       |
       +-- hold released                 +-- availability released

PENDING_APPROVAL --hold expiry--> EXPIRED
       +-- hold released; client notified
```

Rules:
- A pending request occupies a temporary reservation until decision or expiry.
- A confirmed appointment occupies its duration plus the applicable scheduling buffer.
- Cancelled, declined, and expired entries no longer reserve time.
- The owner may edit/cancel confirmed appointments; changes are audited and the client is notified when relevant.
- Rescheduling is modeled as a replacement pending request while retaining the existing confirmed appointment until the replacement is approved. If the replacement is approved, confirm it and cancel the old appointment in one transaction; if declined/expired, leave the original appointment intact.

## 6. Availability and conflict handling

For an appointment candidate:
1. Determine the candidate’s duration from appointment override, otherwise client default.
2. Select the applicable buffer policy (MVP default: configured fixed buffer between homes).
3. Validate the candidate start is within configured business hours and the 14-day booking horizon.
4. Compute the occupied interval according to the agreed buffer semantics.
5. Exclude intervals intersecting confirmed appointments, active pending holds, or unavailable blocks.
6. Return only valid candidate start times.
7. On request creation and approval, repeat validation inside a transaction to handle races.

Use the single-crew DynamoDB calendar-revision transaction described in [ARCHITECTURE.md](ARCHITECTURE.md#5-data-architecture-and-conflict-correctness). The owner confirmed one crew, configurable 1/2/3-hour category defaults, a current 3-hour maximum, and 15-minute start increments. The maximum duration and buffer are configured before booking writes; they bound the lookback query. The 30-minute travel buffer applies between visits only, with no opening or closing boundary buffer. All calendar mutations, including edits, expiry, and reschedule swaps, follow the same transaction discipline.

All timestamps should be stored in UTC and rendered in the configured business timezone. Daylight-saving transitions, local working hours, and date-only client phrases require explicit timezone-aware parsing and tests.

## 7. Key workflows

### 7.1 Client asks for a booking
1. Provider delivers inbound SMS webhook.
2. API verifies signature, deduplicates, and persists event.
3. Conversation service identifies client and interprets request.
4. If required data is missing, assistant asks a concise question.
5. Scheduling service returns valid options using current calendar state.
6. Client selects an option; scheduling service validates and atomically creates a pending request with configured expiry.
7. Outbox emits owner notification and client acknowledgement that the request is awaiting approval.

### 7.2 Owner approves or declines
1. Owner reply is persisted and associated with the pending request/action.
2. Conversation service interprets approval/decline intent and resolves reference.
3. Ambiguity results in a clarification question; no transition occurs.
4. Scheduling service checks allowed transition, expiry, and current conflict state transactionally.
5. State changes; audit event and notifications enter outbox in the same transaction.
6. Worker sends client and owner confirmations; provider delivery status is recorded.

### 7.3 Hold expiration
1. Worker selects expired pending requests using a safe claim/lock mechanism.
2. Scheduling service transitions each eligible request to `EXPIRED` transactionally.
3. Hold is released and client notification is enqueued.
4. Repeated job execution is idempotent.

### 7.4 Cancellation
1. Identify client and target appointment; ask for clarification if multiple appointments could match.
2. Scheduling service verifies ownership/eligibility and cancels transactionally.
3. Calendar availability is released; audit event and owner notification are enqueued.
4. Assistant confirms cancellation only after persisted success.

## 8. AI boundaries and safeguards

- Use structured schemas for intents/tool arguments; validate all values server-side.
- LLM output is untrusted input. Never execute free-form SQL, code, or arbitrary URLs from model output.
- No model-generated direct database writes.
- Scheduling service enforces availability, authorization, booking horizon, durations, approval policy, and state transitions.
- Natural-language owner approval must map to a specific pending request; ambiguity means no action.
- Ask before storing uncertain information as a lasting client note; keep a review status for note drafts if note extraction is enabled.
- Provide human fallback for unknown numbers, unsupported requests, repeated model/tool errors, and sensitive issues.
- Maintain a conversation/action audit trail without unnecessary sensitive text retention.
- Configure provider-level opt-out/help behavior and business messaging consent practices before sending production SMS.

## 9. Security, privacy, and reliability

- HTTPS for all external endpoints; verify SMS provider signatures.
- Secure authentication for the owner interface; strong session handling and CSRF protection where applicable.
- Least-privilege service credentials; secrets stored in managed secret storage/environment configuration, never in prompts or logs.
- Encrypt data in transit and at rest through deployment platform/database capabilities.
- Restrict access to client details and notes; avoid showing one client’s information to another.
- Exclude entry/access codes from the MVP; a later protected workflow requires a separate owner decision and design.
- Idempotent webhook processing and outbound delivery retries.
- Backups and restore tests appropriate to a small business production service.
- Monitor webhook failures, SMS delivery errors, failed scheduling operations, expired holds, and unusual conflict attempts.
- Implement the issue #16 retention periods and data export/deletion practices before onboarding real clients.

## 10. Deployment and operations (initial direction)

For the pilot, use AWS account `339713090487` in `us-west-1` with the proposed Lambda, DynamoDB on-demand, and SQS architecture. The first proof of concept includes the authenticated owner calendar. Use Twilio for the California-only SMS pilot; business-number and campaign approval are pending. Keep a staging environment and test with synthetic contacts before enabling real client traffic. This choice does not authorize resource provisioning, deployment, or live SMS.

Operational essentials:
- Health/readiness endpoint and structured, redacted logs.
- Alerts for inbound webhook outage, outbound SMS failures, and worker backlog.
- DynamoDB schema evolution and rollback/backup plan.
- Configuration for business timezone, hours, hold duration, buffer, and category durations.
- Manual emergency procedure for pausing the assistant and reverting to ordinary owner texting.

## 11. Testing strategy

- Unit tests for time-zone-aware working hours, booking horizon, duration, buffers, status transitions, and expiry.
- Property/concurrency tests proving no overlapping active reservations can be committed.
- Integration tests for scheduling transactions and outbox delivery semantics.
- Webhook tests for signature validation, duplicate delivery, out-of-order events, and provider retry.
- Conversation evaluation with synthetic cases: clear requests, ambiguous dates, competing pending requests, cancellation, reschedule, unknown phone, prompt injection, sensitive details, and unsupported requests.
- Human approval tests confirming ambiguous owner replies never change state.
- End-to-end tests using a messaging-provider sandbox/test numbers before live pilot.
- Accessibility/usability check of the owner’s minimal interface on a phone.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Double booking due to concurrent requests | Strong calendar reads and a conditional revision update in every calendar transaction |
| Owner reply applied to wrong request | Explicit request references, ambiguity detection, confirmation, audit log |
| AI claims a booking without a successful write | Only confirm after scheduling service returns persisted result |
| One-day holds block useful availability | Configurable expiry, owner visibility, automatic release and client notice; review pilot metrics |
| Duration estimates are inaccurate | Owner-configurable mapping and per-appointment override; collect corrections |
| Travel buffer insufficient for distant homes | Fixed configurable MVP buffer; dynamic travel estimate is a later phase |
| Owner cannot use admin UI | SMS-first workflow plus usability-tested minimal calendar; emergency manual fallback |
| Sensitive entry details exposed | Exclude credentials from ordinary notes; define dedicated controls before any storage |
| SMS provider delays/outages | Persistent inbound processing, outbox retries, delivery status, manual fallback |
| SMS compliance/consent problems | Select provider and launch geography early; implement consent and STOP/HELP handling |

## 13. Pilot decisions and remaining setup

1. `America/Los_Angeles` with daylight saving, Monday–Friday 8:00 a.m.–5:00 p.m.; observed US federal holidays closed by default, with owner-editable closures and exceptions.
2. Small/medium/large defaults of 1/2/3 hours, a current maximum of 3 hours, and 15-minute increments are confirmed and configurable.
3. Configurable 30-minute travel buffer between visits only; none before the first or after the last.
4. Reschedule rule: retain the original confirmed appointment until the replacement is approved; atomically swap them on approval (owner confirmed in issue #3).
5. The first proof of concept includes the authenticated owner calendar; its unavailable-block flow is the fallback for owner corrections.
6. California-only pilot SMS uses Twilio and the documented in-person consent process; business-number/campaign approval remains pending. STOP/HELP behavior and the owner-approved 90-day message, 12-month ordinary-note, and four-year minimal consent/opt-out evidence periods must be implemented before live messaging.
7. Entry/access codes are excluded from this MVP.
8. One crew/resource is confirmed for the pilot; revisit only if staff capacity changes.

## 14. Effort framing

Effort depends heavily on provider choice, developer experience, deployment/security expectations, and whether the owner web view is included. The major work streams are:

- Scheduling domain and concurrency-safe calendar: medium.
- SMS integration, delivery, and owner reply routing: medium.
- Reliable natural-language conversation and ambiguity handling: medium-to-high, requiring iteration.
- Minimal owner admin interface and authentication: medium.
- Privacy/compliance, deployment, observability, and operational setup: medium.
- Recurring schedules and dynamic routing: explicitly deferred and materially expand scope.

A realistic estimate should follow discovery of provider/geography, owner interface expectations, working days/timezone, and duration rules. A prototype using sandbox SMS and synthetic clients should precede production commitments. The pilot should retain owner approval throughout.
