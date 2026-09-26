# Scheduling service contracts

**Status:** implementation contract, 2026-09-26  
**Related:** [PRD](PRD.md), [HLD](HLD.md), [data architecture](ARCHITECTURE.md#5-data-architecture-and-conflict-correctness), [issue #3](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/3)

The scheduling service is the sole writer of appointments, holds, calendar blocks, audit events, and notification intents. SMS and owner UI call the same operations. Every successful state-changing operation returns the persisted state and its calendar revision; callers must not announce success before that response. Times are RFC 3339 UTC instants at the boundary and are rendered in the configured business timezone. Intervals are half-open `[start_at, end_at)`.

## Shared rules

- One business and one crew are assumed for the pilot. Do not accept booking writes until timezone, working days/exceptions, maximum visit duration, slot increment, and between-visit buffer policy are configured from issue #1. The service validates horizon, local working hours, duration cap, and conflicts on every calendar-changing write.
- An active reservation is a `CONFIRMED` appointment, a `PENDING_APPROVAL` request with `hold_expires_at > decision_at`, or an unavailable block. Terminal appointments do not occupy time. Store duration and buffer snapshots on each request/appointment. Ignoring expired holds in availability does not require deleting them first.
- Every command includes `business_id`, authenticated actor context, and `idempotency_key`. The boundary derives actor identity from a verified client phone, verified owner SMS number, authenticated owner session, or internal job identity; the caller cannot assert its own role. Use a unique record keyed by `(business_id, actor_id, operation, idempotency_key)` and store a canonical request hash and final response atomically with the change. Same key and payload returns the saved response; same key with different payload returns `IDEMPOTENCY_KEY_REUSED`.
- Owner decisions require one exact pending request ID or unique short reference. An ambiguous natural-language reply produces `CLARIFICATION_REQUIRED` and **no write**. Client cancellation/reschedule likewise requires one unambiguous appointment. An LLM interpretation is never authorization.
- Every committed change writes a minimal audit event and required notification intents in the same DynamoDB transaction. These intents are delivered at least once and have stable IDs. A no-op replay creates no new event. If the transaction conflicts, re-read and retry a bounded number of times; return `SLOT_CONFLICT`, `STALE_VERSION`, or the current terminal state instead of partially applying a command.
- The API may expose `GET /availability` for suggestions. Suggestions are advisory and never reserve time. A returned slot is revalidated by `create_hold` and, where applicable, `approve`.

## Operations and transitions

| Operation | Authorized actor | Preconditions and atomic effect | Notification intents |
|---|---|---|---|
| `create_hold` | Verified client for self, owner for a client | Valid policy, free slot, no existing conflicting active reservation; put `PENDING_APPROVAL`, expiry, event, revision, idempotency result, audit. A replacement hold includes `replaces_appointment_id` owned by the same client and may overlap **only that original**. Only one active replacement per original is allowed. | Owner request summary; client pending acknowledgement. Neither says confirmed. |
| `approve` | Owner | Exact pending ID, expected version, `hold_expires_at > decision_at`, no conflict with other active reservations; change to `CONFIRMED`. For a replacement, require the original still `CONFIRMED`; in the **same transaction** cancel it and remove its event while confirming the replacement. | Client confirmation of new time and, for a replacement, cancellation of old time in one clear message; owner action receipt. |
| `decline` | Owner | Exact active pending ID and version; set `DECLINED`, remove its active event, advance revision. Original appointment of a replacement is untouched. | Client decline with original time retained when applicable; owner receipt. |
| `expire` | System job | Pending with `hold_expires_at <= decision_at`; condition on current status/version, set `EXPIRED`, release event, advance revision. Safe to retry after approval or another expiry worker. Original appointment of a replacement is untouched. | Client expiry with original time retained when applicable. |
| `cancel` | Client for own appointment, owner | Exact confirmed appointment and version; set `CANCELLED`, release event, advance revision. Cancellation of a pending request uses explicit withdrawal with the same release rules and `CANCELLED` status. If the confirmed appointment has an active replacement request, require clarification/withdrawal of that request first; no orphan replacement may later swap a different appointment. | Client cancellation receipt; owner notice for client cancellation. |
| `reschedule` | Verified client for self, owner for a client | Alias of `create_hold` with `replaces_appointment_id`; original remains `CONFIRMED` until `approve` succeeds. A failed, declined, or expired replacement never changes it. | Same as `create_hold`, with explicit original time retained text. |
| `block_time` / `edit_block` | Owner | Validate interval; create, move, or remove an unavailable block using expected version and calendar revision. A conflicting block creation/move fails without altering old state. | Owner receipt; affected clients only if a separately authorized appointment change is committed. |
| `create_owner_appointment` / `edit_appointment` | Owner | Owner can create a confirmed appointment or change start/duration using expected version and the same conflict validation. A duration increase or move that conflicts is **rejected while the existing confirmed appointment remains unchanged**. Profile/config edits never silently rewrite existing duration snapshots. | Client change notice after commit, including new time/duration; owner receipt. No change notice on rejection. |

`decision_at` is taken by the scheduling service immediately before its transaction and included in the conditional expiry check. The transaction's conditional status/version checks serialize approval and expiry workers. The service checks the clock again before submitting an approval transaction if preparation took time; callers must not infer approval from an earlier availability read.

## API envelope and examples

Internal scheduling commands use typed request/response schemas. An owner HTTP adapter can expose `POST /v1/requests`, `POST /v1/requests/{id}/approve`, `POST /v1/requests/{id}/decline`, `POST /v1/appointments/{id}/cancel`, `PATCH /v1/appointments/{id}`, and `POST /v1/blocks`. Provider webhooks are separate and verify signatures before calling the service. Mutating HTTP calls require an `Idempotency-Key` header and server-derived actor context.

Create a replacement hold (the original stays confirmed):

```http
POST /v1/requests
Idempotency-Key: sms-SM123-request
Content-Type: application/json

{"client_id":"client-42","start_at":"2026-10-06T16:00:00Z","duration_minutes":180,"replaces_appointment_id":"appt-9"}
```

```json
{"request_id":"req-12","status":"PENDING_APPROVAL","start_at":"2026-10-06T16:00:00Z","end_at":"2026-10-06T19:00:00Z","hold_expires_at":"2026-10-05T16:00:00Z","replaces_appointment_id":"appt-9","original_status":"CONFIRMED","calendar_revision":81}
```

Approve an exact request:

```http
POST /v1/requests/req-12/approve
Idempotency-Key: owner-SM456-approve
Content-Type: application/json

{"expected_version":1}
```

```json
{"request_id":"req-12","status":"CONFIRMED","replaced_appointment_id":"appt-9","replaced_status":"CANCELLED","calendar_revision":82}
```

Conflicting duration edit (no state change, no notification):

```http
PATCH /v1/appointments/appt-9
Idempotency-Key: owner-edit-17
Content-Type: application/json

{"expected_version":4,"duration_minutes":300}
```

```json
{"error":{"code":"SLOT_CONFLICT","message":"The longer visit conflicts with another reservation."},"current":{"appointment_id":"appt-9","status":"CONFIRMED","duration_minutes":180,"version":4}}
```

Adapters map domain errors consistently: `CLARIFICATION_REQUIRED`/`POLICY_NOT_CONFIGURED` to a question or setup prompt, `SLOT_CONFLICT` to 409 with fresh options, `STALE_VERSION` to 409 with current state, `HOLD_EXPIRED` to 409, `FORBIDDEN` to 403, and `IDEMPOTENCY_KEY_REUSED` to 409. Internal expiry does not use an HTTP route. No API accepts an arbitrary notification destination or a caller-supplied `approved_by`.
