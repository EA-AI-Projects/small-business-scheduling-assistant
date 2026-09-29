# Small Business Scheduling Assistant

An SMS-first scheduling assistant for a small home-cleaning business. Clients request, reschedule, and cancel visits by text; the owner approves new requests by SMS. A shared scheduling system is the source of truth for availability and appointment status.

## Project documents

- [Product Requirements Document (PRD)](doc/PRD.md)
- [High-Level Design (HLD)](doc/HLD.md)
- [Technical Architecture](doc/ARCHITECTURE.md)
- [Pilot infrastructure plan](doc/PILOT_INFRASTRUCTURE.md)
- [Local model evaluation and secrets](doc/MODEL_EVAL.md)
- [Scheduling service contracts](doc/SCHEDULING_CONTRACTS.md)
- [Human–agent team agreement](doc/TEAM_AGREEMENT.md)

## Current MVP direction

- Flexible, one-off appointment requests (recurring schedules deferred).
- Owner approval required before new appointments are confirmed.
- Configurable pending-request hold, defaulting to 24 hours.
- Booking horizon of 14 days.
- Monday–Friday, 8:00 a.m.–5:00 p.m. in `America/Los_Angeles`; observed US federal holidays are closed, with owner-editable exceptions.
- Configurable small/medium/large visit defaults of 1/2/3 hours, currently capped at 3 hours, with an owner override per appointment.
- Configurable 15-minute start-time increments and one crew for the pilot.
- Configurable 30-minute travel buffer between visits, with none before the first or after the last.
- SMS-first owner and client workflows, with a minimal owner calendar/admin view.
- Free client cancellations; cancelled time becomes available for future requests.

## Status

The backend exposes a health endpoint and read-only calendar and availability APIs backed by a synthetic local adapter. The owner-approved pilot policy is encoded as a default for local tests; the DynamoDB adapter requires the owner policy seed before booking. Hold creation, appointment transitions, owner policy edits, blocks, and manual appointments use revision-guarded atomic writes. Provider-independent outbox dispatch and delivery logic includes a scheduled Lambda dispatcher entry point. A Twilio ingress and outbox sender, authenticated owner consent record, delivery-status callback, and 90-day SMS body purge worker are implemented with synthetic tests; queue/API resources and periodic triggers remain to be provisioned in #22. An authenticated owner API, client profile/note storage, and a minimal owner calendar page are implemented but not deployed. No production scheduling service has been deployed or live SMS sent.

## Local backend

Python 3.12+ is required locally. From the repository root:

```sh
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e './backend[dev]'
backend/.venv/bin/python -m uvicorn scheduling.api:app --app-dir backend --reload
```

Then open `http://127.0.0.1:8000/docs` or request `GET /health`. `GET /v1/businesses/pilot/calendar` requires timezone-aware `start_at` and `end_at` query parameters. `GET /v1/businesses/pilot/availability` accepts `day` and `duration_minutes`, and returns UTC starts under the owner-approved pilot policy. The in-memory calendar contains no customer data and resets on restart. These read endpoints are a local harness, not authenticated production routes.

To preview how the model interprets your own fictional texts, use the [interactive model preview](doc/MODEL_EVAL.md) with `--interactive`. It proposes an intent and clarification question but cannot book a visit or send an SMS.

## Owner command API

`scheduling.owner_auth.create_cognito_owner_app` constructs a separate owner API with a DynamoDB repository and Cognito access-token verification. Its caller must supply the DynamoDB client/table, Cognito issuer, app-client ID, configured owner subject, and business ID. The verifier checks the token signature, issuer, expiry, app client, token type, and exact owner subject; every owner route also checks the business in the path. The hosting layer must expose only this authenticated app for non-local owner access and supply these settings securely. No owner route is mounted by the synthetic `scheduling.api:app` harness.

Owner routes are under `/v1/owner/businesses/{business_id}`. They list pending requests and the calendar, seed/read/edit scheduling policy, create requests, approve or decline an exact request, cancel or edit an appointment, create/move/remove unavailable blocks, and create a confirmed manual appointment. They also list and update client profiles, create/list/delete ordinary client or appointment notes, and set/release a documented legal hold on a note. Every mutation requires `Authorization: Bearer <Cognito access token>`. Scheduling commands and note creation additionally require `Idempotency-Key`; scheduling edits and decisions carry expected versions or calendar revisions. Scheduling-command replays with the same key and body return the original committed result. Client profile writes use expected versions and return a conflict on retry after a successful write; callers should read the current profile before retrying. Note creation uses a stable ID from the key and rejects the same key with different content. Note deletion returns not found on retry after a successful delete. The owner UI and AWS integration are tracked separately in #19 and #23.

The owner cannot mark a phone verified through HTTP. Unverified phones are not returned by verified-phone lookup. The signed SMS ingress maps only already verified client phones to scheduling actor context; phone verification enrollment remains a separate trusted step. The owner may record an in-person clear yes and script version at `POST .../clients/{client_id}/sms-consent`; this does not send an enrollment text. Outbound SMS also requires a verified phone, consent for clients, no local opt-out for either recipient, and an explicitly authorized recipient allowlist. Twilio Advanced Opt-Out handles STOP/HELP replies; local STOP remains in force until a newer in-person consent and START are both recorded. The SQS sender requires `SMS_SEND_ENABLED=authorized` and `AUTHORIZED_SMS_RECIPIENTS` before it can send. These settings do not replace business-number/campaign approval or separate test-number authorization. Provider acceptance marks an outbox intent sent; a later signed delivery callback is linked to the outbox ID. Terminal delivery failures are visible through the authenticated owner `GET .../sms-delivery-failures` route for manual follow-up; they do not trigger a blind resend. A provider timeout after acceptance can still cause a duplicate text on retry.

Ordinary notes reject recognized entry/access-code phrases. The authenticated owner can set a legal hold with a documented reason through `PATCH .../clients/{client_id}/notes/{note_id}/legal-hold` and release it with `{"reason": null}`. A conditional write prevents a hold update from racing with note deletion. A separate `scheduling.workers.note_retention` handler deletes expired notes; #23 must schedule it before real client data is onboarded. For retention, a confirmed appointment counts as a completed visit once its end time passes, unless cancelled. The handler finds the latest such visit through a strongly consistent appointment scan for the one-business pilot. This favors deletion correctness at low pilot volume; revisit the access pattern if client volume grows. A documented legal hold prevents automatic deletion; release makes an overdue note eligible for the next purge.

The `scheduling.workers.sms_retention` handler removes SMS bodies 90 days after each sender's last scheduling exchange, including accepted outbound replies, while retaining receipt IDs for deduplication. It also deletes minimal consent and immutable STOP evidence four calendar years after the phone's last provider-verified inbound client/owner text (including STOP) or accepted outbound program text; consent with no text uses its agreement date. An active STOP retains a minimal phone-number suppression marker beyond historical evidence expiry until a fresh consent followed by START clears it; evidence deletion cannot silently enable delivery. Existing STOP records without a marker are treated as active, and the purge creates a marker atomically before deleting such a record. Existing threads without the new evidence clock fall back to their last exchange timestamp. `DynamoSmsIngressStore.set_evidence_legal_hold` is a trusted administrative method for a documented hold on a specific evidence record. #23 must schedule the retention handler (for example, daily through EventBridge), wire a separately authorized hold-management path, and monitor failures before real SMS data is onboarded; neither handler deploys itself.

### Owner calendar page

When `create_cognito_owner_app` receives both `ui_domain` and `ui_redirect_uri`, it serves the owner page at `/owner` on the same origin as the authenticated API. Configure a Cognito public app client with authorization-code grant, PKCE, the `openid` scope, and an exact HTTPS callback URI ending in `/owner`; pass its hosted UI domain and callback URI to the app factory. The page uses the [Cognito authorization-code PKCE flow](https://docs.aws.amazon.com/cognito/latest/developerguide/using-pkce-in-authorization-code.html), keeps the access token in page memory only, and sends it only to the same-origin owner API. A refresh requires signing in again. The owner token verifier still checks the exact configured owner subject and business ID. No browser-supplied role or business ID grants access.

The page provides day/week schedule and pending approvals in the business timezone, appointment edits/cancellation, unavailable blocks, date exceptions, client profiles, and separate client/visit notes. Local date-time inputs are resolved by the authenticated API and reject ambiguous or nonexistent daylight-saving times. A conflicting edit refreshes current state and states that nothing was saved. The page and its public OAuth configuration contain no customer data or credentials; data is returned only through authenticated owner routes. Issue #23 must provide the hosted Cognito client, same-origin routing, and deployment settings; this code does not provision or publish them.

Focused checks:

```sh
backend/.venv/bin/python -m pytest backend/tests
backend/.venv/bin/python -m ruff check backend
backend/.venv/bin/python -m mypy backend/scheduling
```
