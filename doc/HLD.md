# High-Level Design: Smart Scheduling Assistant

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
- Answer a verified client's calendar questions read-only (#240, #271): the model interprets the question in any wording or language and may only propose the `calendar_question` or `clarify_booking` intent with a day range, statuses, and list or count. The backend reads only that client's current visits, then produces the authoritative answer from `client_calendar_questions.py`; no other client's visits, owner blocks, or notes enter the tool result. A long list becomes a compact SMS that still names every current visit's local start date, time, and status. The model may add a short conversational wrapper but must keep the backend answer verbatim. Follow-ups (#241) reuse only the last question's range, statuses, and view, kept 30 minutes in the per-sender conversation memory, and reread the visits each time. See [CONVERSATION.md](CONVERSATION.md#client-calendar-questions).
- For verified client availability questions (#271), the typed model proposal selects the sender-scoped `list_available_slots` read. The backend checks current policy, duration, horizon, blocks, visits, and holds before returning an answer. The model gets the bounded sent-aware 24-hour SMS transcript and open prompt kind to interpret date-only follow-ups. A second model call may draft one SMS around the authoritative result; the service rejects drafts that change or add scheduling claims. Failed drafts use the authoritative answer.
- Client cancellation and rescheduling (#273) use the same typed pattern as `request_booking`. The model proposes `cancel` or `reschedule` with the day of the visit; the backend resolves it only among the sender's own active visits, lists them by number if more than one matches, and asks the existing 30-minute cancel confirmation or offers replacement times. The model then reads the client's answer in any wording and proposes `confirm_cancel`, `keep_visit` (cancellation) or `request_booking` (replacement). It names no visit: the backend acts only on the sender's own unexpired stored prompt (same client ID), which already binds one appointment and version, and calls the existing lifecycle or hold service with the inbound receipt ID as idempotency key. A replacement creates a pending request and the original stays confirmed until the owner approves it. An expired, replaced, changed-version, conflicting, or another client's prompt writes nothing. Plain YES and option-number matching still run ahead of the model, and client replies still use the deterministic backend texts; model-authored replies for these results are a separate follow-up.
- Never tell a client that a booking is confirmed unless the scheduling service returns a persisted confirmed state.
- Owner conversation (#274): the verified owner's texts go through the same typed-tool boundary. After the deterministic matchers, one model classification may call `show_requests` (read), `prepare_counteroffer`, or name a request for approve/decline, always with a reference and the request version it read. The backend writes only if that reference and version are the stored, current ones for this business, and a counteroffer is only drafted: the owner's plain YES still sends it, and approve or decline still needs the exact command or the single-request plain reply. A mismatched or missing version (which only reveals a model copy error or stale snapshot) never acts. Duplicate or failed calls get a truthful backend reply and change nothing. Replies stay the backend's text; model-authored owner replies are a later change. See [CONVERSATION.md](CONVERSATION.md#owner-tools-274).
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

For the future scheduled booking invitations in [CONVERSATION.md](CONVERSATION.md#scheduled-booking-invitations-249), keep selection outside the LLM and the ordinary inbound conversation handler. Authenticated owner Settings saves a separate, versioned booking-outreach record with enabled, local weekday/time, and one- or two-week lookahead. A new business reads a disabled, unconfigured version-zero default. A selector checks the current setting at the exact local run minute (first occurrence at a fall-back; no catch-up for a spring gap or missed run), then scans active profiles, current matching in-person consent and opt-out state, and confirmed appointments overlapping the inclusive lookahead endpoint. It writes a per-client intent and guard in one conditional transaction; the guard blocks overlapping runs and records last successful handoff time. The intent stores a phone hash and verification instant, not the phone or message body. A separately gated worker promotes eligible intents into the existing SMS outbox; the sender rechecks current settings, eligibility, consent, opt-out, verified phone, and appointment state immediately before provider handoff. Suppressed intents are terminal. A provider attempt with uncertain acceptance remains held for reconciliation rather than retried. Replies use the existing booking flow. The owner approved the sender label and confirmed disclosure and campaign coverage on #252; separate live-SMS authorization is still required. The schedule and invitation delivery gate default to disabled.

The versioned outreach settings also store one owner-editable message shared by scheduled and manual sends. Legacy records without a message render the approved default. New scheduled intents capture the settings version, and any settings edit before handoff suppresses that queued intent. The Settings UI saves edited text before an immediate run. The API accepts nonblank text up to 500 characters without a STOP-content requirement; consent and opt-out checks remain in the delivery path.

Manual invitations use the same eligibility and send-time checks but ignore the automatic enabled flag and repeat history. Each owner click processes clients in pages under one retryable request key and cursor, reserving client-linked intents and outbox records without a per-client frequency guard. The browser retains the key, text, and cursor until every page completes. The custom message is stored on the intent for delivery; the daily retention job removes it at 90 days, with DynamoDB TTL as a fallback. The owner API and SMS sender require a separate manual-delivery authorization flag and the SMS retention schedule, both disabled by default; the ordinary live-SMS gate also applies. The owner app previews the eligible count before a confirmed send.

- Send concise SMS notifications for pending requests and cancellations.
- Include a unique, non-guessable or otherwise securely scoped request reference when needed to disambiguate concurrent requests.
- Resolve owner replies against a pending action. If more than one action matches, ask the owner to select/clarify; do not mutate state.
- Natural-language parsing can identify intent, but the scheduling service validates that the requested transition is allowed.
- Send owner a result receipt after the operation.

### 3.5 Owner web admin

- Authenticated, mobile-friendly minimal interface for calendar/list review, pending requests, clients, unavailable blocks, and configuration. Use Cognito authorization-code PKCE in the browser, keep the access token in page memory (the ID token is read once for the display-only `email` claim, not signature-checked, and dropped; the scope stays `openid`), and serve the page on the same origin as the owner API. The API resolves owner-entered local date-times and rejects daylight-saving gaps or ambiguous times before calendar writes.
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
- Delete ordinary client and appointment notes 12 months after the last completed visit, except during a documented legal hold. An authenticated owner can set or release the hold with a reason on an existing note; a conditional write prevents a race with purge. An overdue note becomes eligible for the next purge after release. A confirmed appointment whose end time has passed counts as completed unless cancelled. For a client with no completed visit, delete each note 12 months after its creation. Reject a new note if the last completed visit was already more than 12 months ago.

### Conversation and Message
- `Conversation`: client/phone association, current conversation state, timestamps.
- `Message`: provider message ID, direction, delivery status, minimized/redacted content where possible, timestamps.
- Delete SMS message bodies 90 days after the last scheduling exchange (in the owner's own thread, 90 days after each message, #201). Keep minimal consent and opt-out evidence separately for four years after the last program text, except during a documented legal hold. Do not copy full message bodies into that evidence record.

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
- Rescheduling is modeled as a replacement pending request while retaining the existing confirmed appointment until the replacement is approved. If the replacement is approved, confirm it and cancel the old appointment in one transaction; if declined/expired, leave the original appointment intact. The same model covers an accepted owner counteroffer (#176), where the original is the client's still-pending request: it stays pending, and approving the replacement confirms it and declines the original in one transaction (an original that already ended is left alone). See [SCHEDULING_CONTRACTS.md](SCHEDULING_CONTRACTS.md#client-acceptance-176).

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

For the pilot, use `us-west-1` (the synthetic `dev` stack in the dedicated member account `214965372605`; `pilot` in its own separate account, see [ARCHITECTURE.md](ARCHITECTURE.md)) with the proposed Lambda, DynamoDB on-demand, and SQS architecture. The first proof of concept includes the authenticated owner calendar. Use Twilio for the California-only SMS pilot; a US number is bought and the A2P 10DLC brand and campaign are approved (issue #91), which does not authorize live SMS. Keep a staging environment and test with synthetic contacts before enabling real client traffic; the only real personal data allowed in `dev` is the authorized testers' phone numbers, messages, and consent evidence (see [ARCHITECTURE.md](ARCHITECTURE.md) section 8.3). This choice does not authorize resource provisioning, deployment, or live SMS.

Operational essentials:
- Health/readiness endpoint and structured, redacted logs.
- Alerts for inbound webhook outage, outbound SMS failures, and worker backlog.
- DynamoDB schema evolution and rollback/backup plan.
- Configuration for business timezone, hours, hold duration, buffer, and category durations.
- Manual emergency procedure for pausing the assistant and reverting to ordinary owner texting.

## 11. Testing strategy

- Keep a small set of in-process workflow checks for booking, cancellation, rescheduling, expiry, duplicate requests, and other state changes where one action affects the next. The retained backend suite checks these workflows and selected HTTP, provider, queue, and DynamoDB boundaries. The retained frontend suite checks owner API requests, generated policy fields, consent-page version, and hosting configuration. These checks use synthetic data and do not replace functional or end-to-end coverage, which is a later effort (owner decision, 2026-10-02; #133).
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
6. California-only pilot SMS uses Twilio and the documented in-person consent process; a US number is bought and the A2P 10DLC brand and campaign are approved (issue #91). STOP/HELP behavior and the owner-approved 90-day message, 12-month ordinary-note, and four-year minimal consent/opt-out evidence periods must be implemented before live messaging.
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
