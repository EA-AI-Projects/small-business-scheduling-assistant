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
- Signed-in clients read the same availability through `GET /v1/client/availability?day=YYYY-MM-DD` (the visit length is the linked client's owner-set profile duration, never caller-supplied; a missing or inactive profile, or a duration the policy rejects, returns 503) and their own live requests through `GET /v1/client/bookings` (#231); identity comes from the verified link and the response carries no other client's or owner-block data.
- A signed-in client requests a visit through `POST /v1/client/requests` with only `start_at` and an `Idempotency-Key` (#233). Identity and duration come from the verified link and profile; the route calls the same `HoldService.create` as the SMS path, so availability is revalidated atomically, the result is a pending hold with the standard owner and client notices and expiry, a replay of the same key returns the same request, and a stale or conflicting start returns `SLOT_CONFLICT`/`CALENDAR_BUSY` with current alternatives and no hold. Like the SMS path it requires an active profile with a verified phone.
- A signed-in client cancels with `POST /v1/client/bookings/{appointment_id}/cancel` (`{expected_version}`) and asks to move a confirmed visit with `POST /v1/client/bookings/{appointment_id}/reschedule` (`{start_at, expected_version}`), each with an `Idempotency-Key` stored as `portal:<key>` (#234). Authorization is the verified link's client and the specific booking; an absent, other-business, or other-client booking is one 404, and a changed version is `STALE_BOOKING`. Cancel is the client `CANCEL` lifecycle command (the SMS path's), so it queues the standard owner and client notices; a confirmed visit with an active replacement cannot be cancelled until the replacement is withdrawn (`REPLACEMENT_PENDING`). A move is a `HoldService.create` hold with `replaces_appointment_id` and `replaces_confirmed_version`: it is revalidated against current availability and hold rules, keeps the original confirmed and its time blocked, and is confirmed only by the owner's approval, which swaps both in one transaction; decline, withdrawal, or expiry clears the replacement guard and leaves the original intact.
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

- `draft_counteroffer(ref, version, date, time, client_text)` is the only owner conversation path that prepares an offer. The model writes `client_text`; the backend resolves one current pending request in the verified owner's business at the supplied reference and version. It runs `check_offer`: the request is still pending and unexpired; the client is active, phone-verified, consented, and not opted out; the proposed time is not the request's own time and is available for its duration snapshot, with that request excluded from its own conflict check. Past, ambiguous, unavailable, ineligible, stale, or otherwise invalid proposals store nothing and return an error. A successful call stores the model's exact client-facing text and a 30-minute `PROPOSED` draft, sends nothing, and returns its ID and details as structured data. A revised instruction replaces the previous draft.
- The model reads the owner's text and 24-hour sent-aware transcript to decide whether to call `send_counteroffer(draft_id)` or `cancel_counteroffer(draft_id)`. A plain YES with an open draft is interpreted as send through the model tool loop, not a deterministic offer hook. The backend accepts either tool only for this verified owner's stored, unexpired draft. Cancel marks it `DISCARDED` and sends nothing. A calendar read leaves the draft open. The deterministic YES/NO offer hook, typed offer instruction parser, closed-offer reminders, and fixed owner counteroffer sentences are removed; the model writes the final owner SMS from the tool result. This wording is not separately checked for meaning, so the model may misread the confirmation or describe a draft incorrectly. The exact client text stored on the draft is the only text the sender can deliver.
- Send requires trusted sent-history evidence that an owner preview containing the exact stored `client_text` was sent after this draft was created. Without it, `send_counteroffer` returns `owner_preview_missing` and queues nothing. It also re-runs `check_offer`, including current request status/version, client eligibility and consent, the proposed slot, and the newer-request guard. It then conditionally moves `PROPOSED` to `CONFIRMED` and queues one client outbox intent (`owner-counteroffer`, ID `counteroffer#<offer>`, event version = confirmed record version) in one transaction, recording the offer as the client's latest confirmed offer. An expired, replaced, wrong-owner, stale-version, or failed draft sends nothing. The model's owner reply must distinguish queued from delivered. Duplicate or stale sends lose the version check and cannot queue a second client text; a redelivered owner SMS replays its original reply.
- On confirmation the record's `expires_at` is reset to confirmation time plus `CLIENT_OFFER_VALIDITY`, which is the shared 30-minute client offer lifetime (`PROMPT_LIFETIME`) that every normal client offer uses. The owner decided on 2026-10-04 (#176) that a counteroffer follows the same rules as a normal offer (PRD section 6.4, #60). Storage TTL is only a retention window and never defines validity.
- The client send reads the model-written text stored on the confirmed draft; it does not ask the model to rewrite it at delivery. It re-runs `check_offer` at send time. On failure no client text is sent; one owner failure notification (`owner-counteroffer-failed`, ID `counteroffer-failed#<offer>`, inserted only if absent) reports why the offer could not be sent and asks for another time, and the client delivery fails permanently with an `OFFER_<problem>` code.
### Owner conversation tools (#298-#300)

The verified owner's strict JSON tool loop exposes `list_pending_requests`, `get_calendar`, `approve_request`, `decline_request`, `draft_counteroffer`, `send_counteroffer`, and `cancel_counteroffer`. The backend derives business and owner identity from the receipt, validates each tool's arguments and stored state, and uses the lifecycle or counteroffer service for every write. Approval and decline use the supplied current request version; a counteroffer draft uses the same version and the send checks it again. The inbound receipt ID and stable outbox IDs preserve idempotency. A duplicate receipt replays the reply. See [CONVERSATION.md](CONVERSATION.md#owner-approval-and-decline-tool-loop-298).

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
