# Scheduling service contracts

**Status:** implementation contract, 2026-09-26

**Related:** [PRD](PRD.md), [HLD](HLD.md), [data architecture](ARCHITECTURE.md#5-data-architecture-and-conflict-correctness), [issue #3](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/3)

The scheduling service is the sole writer of appointments, holds, calendar blocks, audit events, and notification intents. SMS and owner UI call the same operations. Every successful state-changing operation returns the persisted state and its calendar revision; callers must not announce success before that response. Times are RFC 3339 UTC instants at the boundary and are rendered in the configured business timezone. Intervals are half-open `[start_at, end_at)`.

## Shared rules

- One business and one crew are confirmed for the pilot. Seed the owner-editable policy with `America/Los_Angeles`, Monday–Friday 8:00 a.m.–5:00 p.m., observed US federal holiday closures, 15-minute starts, a 3-hour visit maximum, and a 30-minute buffer only between visits. Do not accept booking writes until that policy is persisted. The service validates horizon, local working hours, duration cap, and conflicts on every calendar-changing write.
- An active reservation is a `CONFIRMED` appointment, a `PENDING_APPROVAL` request with `hold_expires_at > decision_at`, or an unavailable block. Terminal appointments do not occupy time. Store duration and buffer snapshots on each request/appointment. Ignoring expired holds in availability does not require deleting them first.
- Every command includes `business_id`, authenticated actor context, and `idempotency_key`. The boundary derives actor identity from a verified client phone, verified owner SMS number, authenticated owner session, or internal job identity; the caller cannot assert its own role. Use a unique record keyed by `(business_id, actor_id, operation, idempotency_key)` and store a canonical request hash and final response atomically with the change. Same key and payload returns the saved response; same key with different payload returns `IDEMPOTENCY_KEY_REUSED`.
- Owner decisions require one exact pending request ID or unique short reference. An ambiguous natural-language reply produces `CLARIFICATION_REQUIRED` and **no write**. Client cancellation/reschedule likewise requires one unambiguous appointment. An LLM interpretation is never authorization.
- Every committed change writes a minimal audit event and required notification intents in the same DynamoDB transaction. These intents are delivered at least once and have stable IDs. A no-op replay creates no new event. If the transaction conflicts, re-read and retry a bounded number of times; return `SLOT_CONFLICT`, `STALE_VERSION`, or the current terminal state instead of partially applying a command.
- The API may expose `GET /availability` for suggestions. Suggestions are advisory and never reserve time. A returned slot is revalidated by `create_hold` and, where applicable, `approve`.

## Operations and transitions

| Operation | Authorized actor | Preconditions and atomic effect | Notification intents |
|---|---|---|---|
| `create_hold` | Verified client for self, owner for a client | Valid policy, free slot, no existing conflicting active reservation; put `PENDING_APPROVAL`, expiry, event, revision, idempotency result, audit. A replacement hold includes `replaces_appointment_id` owned by the same client and may overlap **only that original**. Only one active replacement per original is allowed. Exception (#176): an accepted owner counteroffer passes `replaces_pending_version`, and the original is then the same client's still-pending, unexpired request at exactly that version instead of a confirmed visit; the owner notification is `counteroffer-request`. | Owner request summary; client pending acknowledgement. Neither says confirmed. |
| `approve` | Owner | Exact pending ID, expected version, `hold_expires_at > decision_at`, no conflict with other active reservations; change to `CONFIRMED`. For a replacement, require the original still `CONFIRMED`; in the **same transaction** cancel it and remove its event while confirming the replacement. For an accepted counteroffer the original is a pending request: it is declined and its event removed in that same transaction, or left alone if it already ended (expired, declined, cancelled). A pending original with an active replacement cannot itself be approved (`ReplacementPending`). | Client confirmation of new time and, for a replacement, cancellation of old time in one clear message; owner action receipt. |
| `decline` | Owner | Exact active pending ID and version; set `DECLINED`, remove its active event, advance revision. Original appointment of a replacement is untouched. | Client decline with original time retained when applicable; owner receipt. |
| `expire` | System job | Pending with `hold_expires_at <= decision_at`; condition on current status/version, set `EXPIRED`, release event, advance revision. Safe to retry after approval or another expiry worker. Original appointment of a replacement is untouched. | Client expiry with original time retained when applicable. |
| `cancel` | Client for own appointment, owner | Exact confirmed appointment and version; set `CANCELLED`, release event, advance revision. Cancellation of a pending request uses explicit withdrawal with the same release rules and `CANCELLED` status. If the confirmed appointment has an active replacement request, require clarification/withdrawal of that request first; no orphan replacement may later swap a different appointment. | Client cancellation receipt; owner notice for client cancellation. |
| `reschedule` | Verified client for self, owner for a client | Alias of `create_hold` with `replaces_appointment_id`; original remains `CONFIRMED` until `approve` succeeds. A failed, declined, or expired replacement never changes it. | Same as `create_hold`, with explicit original time retained text. |
| `block_time` / `edit_block` | Owner | Validate interval; create, move, or remove an unavailable block using expected version and calendar revision. A conflicting block creation/move fails without altering old state. | Owner receipt names the affected local date, start and end times, and timezone from the committed interval; affected clients only if a separately authorized appointment change is committed. |
| `edit_business_calendar` | Owner | Change operating windows, holiday date exceptions, or duration/buffer caps using the expected calendar revision. Reject a new closure containing a confirmed visit and a lower cap that would exclude an active visit/hold snapshot; resolve those reservations explicitly first. | Owner receipt; no client notice on rejection. |
| `create_owner_appointment` / `edit_appointment` | Owner | Owner can create a confirmed appointment or change start/duration using expected version and the same conflict validation. A duration increase or move that conflicts is **rejected while the existing confirmed appointment remains unchanged**. Profile/config edits never silently rewrite existing duration snapshots. | Client change notice after commit, including new time/duration; owner receipt. No change notice on rejection. |

`decision_at` is taken by the scheduling service immediately before its transaction and included in the conditional expiry check. The transaction's conditional status/version checks serialize approval and expiry workers. The service checks the clock again before submitting an approval transaction if preparation took time; callers must not infer approval from an earlier availability read.

## Owner counteroffer confirmation (#175)

A counteroffer is not a calendar operation: it writes no appointment, hold, or revision and the original request stays `PENDING_APPROVAL`. It is a versioned confirmation record (`Counteroffer`, states `PROPOSED`, `CONFIRMED`, `DISCARDED`) linked to the request ID and version, the client, the proposed start, and the exact client-facing text.

- Preparation resolves one pending request from an exact reference, a unique client name, a single pending request, or a unique date; otherwise it asks which request and stores nothing. It then runs `check_offer`: request still pending at the recorded version and unexpired, client profile active and phone-verified, consent present and not opted out, proposed time not equal to the request's own time, and the time offered by availability for the request's duration snapshot (the request being countered does not block its own offer). Failure explains and asks for another time.
- While proposed, the record lasts 30 minutes (a technical default for the owner prompt). A plain YES confirms. NO or CANCEL discards it and the request stays pending. A new offer instruction replaces it. An exact `APPROVE <ref>` or `DECLINE <ref>` clears it and runs normally. Any other reply no longer cancels the offer (#177): the owner's text is routed by the model-based classification in [CONVERSATION.md](CONVERSATION.md#owner-message-routing), where an open offer can only be confirmed, cancelled, revised, or clarified, and never approves the request. A calendar question during an open offer is answered and the offer stays open (the reply says it is still waiting). Directly after a calendar answer only an unmistakable YES sends the offer, and an "instead" without an offer verb is clarified instead of prepared. Approval-like wording ("Yes, approve it", "Go ahead", "Send it") never approves the countered request; the backend refuses it and repeats what a YES or an exact `APPROVE <ref>` does. The owner's pointer to the offer (confirmed, send-failed, cancelled, or expired) is kept for one hour after its expiry. While that offer's request is still the same current pending request, a bare yes/ok/no is absorbed with "nothing was approved" (or "could not be sent" after a send-time failure) and writes nothing; other replies are never approvals either, and only an exact `APPROVE <ref>` or `DECLINE <ref>` acts on the request until the pointer is cleared (by a calendar answer, a new offer, or the hour passing). Once the request is no longer that pending request, the pointer is cleared and replies are handled normally.
- Confirmation re-runs `check_offer`, then in one transaction moves `PROPOSED` to `CONFIRMED` at the expected version, queues one client outbox intent (`owner-counteroffer`, ID `counteroffer#<offer>`, event version = confirmed record version), and records the offer as the client's latest confirmed offer. The owner is told the offer is queued, not delivered. Duplicate or stale confirmations lose the version check and send nothing; a redelivered owner SMS replays its original reply.
- On confirmation the record's `expires_at` is reset to confirmation time plus `CLIENT_OFFER_VALIDITY`, which is the shared 30-minute client offer lifetime (`PROMPT_LIFETIME`) that every normal client offer uses. The owner decided on 2026-10-04 (#176) that a counteroffer follows the same rules as a normal offer (PRD section 6.4, #60). Storage TTL is only a retention window and never defines validity.
- The client send reads the stored text (never model or caller text) and re-runs `check_offer` at send time. On failure no client text is sent; one owner text (`owner-counteroffer-failed`, ID `counteroffer-failed#<offer>`, inserted only if absent) says the offer could not be sent, why, and asks for another time, and the client delivery fails permanently with an `OFFER_<problem>` code.
### Owner conversation tools (#274)

The model-led owner loop adds no write operation. Its tools are proposals checked against stored state: `show_requests` is a read of pending requests; `prepare_counteroffer` is accepted only for exactly one current pending request whose reference matches and whose `version` equals the request's stored version, and it only creates the `PROPOSED` confirmation record described above (owner confirmation, expiry, and the confirmation-time version and newer-request checks are unchanged); `approve_named_request` and `decline_named_request` quote reference and version, never act on a stale version, and with several pending requests only ask for the exact `APPROVE <ref>` / `DECLINE <ref>` command. The `approve` and `decline` operations above are still reached only through that command or the plain-reply allowlist for the single pending request, with the owner's inbound receipt ID as idempotency key and the request's current version as the expected version. See [CONVERSATION.md](CONVERSATION.md#owner-tools-274).

## Client acceptance (#176)

`CounterofferAcceptance` reads the client's own latest confirmed offer through `read_confirmed_for_client` (the sender must be that client at the offer's phone). Offer states add `ACCEPTED` (a request was created; names it) and `SUPERSEDED` (the client declined, answered after expiry, or made a new request).

- Matching reuses the normal-offer rules with the single offered time as the only option: a plain yes, or the offered time, accepts (while a client calendar conversation is open, only a plain YES or NO reaches the offer, see [CONVERSATION.md](CONVERSATION.md#client-calendar-questions); other replies there are calendar follow-ups or go to the model, which may read a time as a new request); a negation, question, alternative, or different time creates nothing. A message after the 30 minutes changes nothing and gets the normal expired-offer reply (request current times). A new client request (booking, availability, reschedule, cancel) supersedes the offer. Other chatter, including a calendar question or follow-up, leaves it open until it expires; a calendar answer ends by naming the open offer and what YES and NO do (#241).
- A repeated YES after acceptance is answered ("nothing more was requested") only while the created request is still pending, the offer's window is open, and the client has no newer prompt (a calendar answer is not one, since it asks nothing); otherwise it is handled like any message, so normal offers and cancel confirmations work. A new client request also ends an accepted offer. If the request exists but the offer update was lost (crash), the redelivered YES finds the request through the acceptance idempotency key, repairs the offer, and replays the success reply before any availability recheck.
- When the original expires while its accepted replacement is still pending, the client gets the normal expiry text plus that the replacement request is still pending owner approval (`expire-replacement-waiting`).
- Acceptance reruns `check_offer` (consent, source request still pending at the recorded version and unexpired, availability), then calls `create_hold` with `replaces_appointment_id` = the source request and `replaces_pending_version` = its recorded version. That is the existing replacement hold: it may overlap only its own original, only one active replacement per original, one idempotency key per offer (`counteroffer-accept#<offer>`) so duplicate or repeated YES replies replay the same single request, revision-guarded. The offer is then marked `ACCEPTED`. Nothing is confirmed and the client is never told it is.
- The new request has its own normal expiry (`hold_minutes`). The original keeps its own and stays `PENDING_APPROVAL`. While the replacement is active the original cannot be approved (`ReplacementPending`), so the client cannot be booked twice; it can still be declined, cancelled, or expire.
- The owner is notified with template `counteroffer-request`: an accepted counteroffer, new request time and reference, the original request time and reference, and that approving it resolves both.
- `approve` of the replacement reuses the replacement swap in one transaction: the replacement becomes `CONFIRMED` and a still-pending original becomes `DECLINED` with its event released. An original that already ended (expired, declined, cancelled) is left alone and the replacement is approved by itself. Notifications are `counteroffer-approved` (the owner text says both requests are resolved) and the owner's immediate reply names both times.
- `decline` or `expire` of the replacement leaves the original pending while its own expiry allows; the decline text (`counteroffer-declined`) says so only when the original is still live at delivery.

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

{"expected_version":4,"duration_minutes":180}
```

```json
{"error":{"code":"SLOT_CONFLICT","message":"The longer visit conflicts with another reservation."},"current":{"appointment_id":"appt-9","status":"CONFIRMED","duration_minutes":120,"version":4}}
```

Adapters map domain errors consistently: `CLARIFICATION_REQUIRED`/`POLICY_NOT_CONFIGURED` to a question or setup prompt, `SLOT_CONFLICT` to 409 with fresh options, `STALE_VERSION` to 409 with current state, `HOLD_EXPIRED` to 409, `REPLACEMENT_PENDING` to 409 (an active replacement from the same client must be approved, declined, or withdrawn first), `INVALID_TARGET` to 409, `RECORD_CONFLICT` to 409, `FORBIDDEN` to 403, and `IDEMPOTENCY_KEY_REUSED` to 409. Internal expiry does not use an HTTP route. No API accepts an arbitrary notification destination or a caller-supplied `approved_by`.
