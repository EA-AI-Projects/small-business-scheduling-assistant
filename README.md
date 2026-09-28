# Small Business Scheduling Assistant

An SMS-first scheduling assistant for a small home-cleaning business. Clients request, reschedule, and cancel visits by text; the owner approves new requests by SMS. A shared scheduling system is the source of truth for availability and appointment status.

## Project documents

- [Product Requirements Document (PRD)](doc/PRD.md)
- [High-Level Design (HLD)](doc/HLD.md)
- [Technical Architecture](doc/ARCHITECTURE.md)
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

The backend exposes a health endpoint and read-only calendar and availability APIs backed by a synthetic local adapter. The owner-approved pilot policy is encoded as a default for local tests; the DynamoDB adapter requires the owner policy seed before booking. Hold creation, appointment transitions, owner policy edits, blocks, and manual appointments use revision-guarded atomic writes. Provider-independent outbox dispatch and delivery logic includes a scheduled Lambda dispatcher entry point; queue resources and the SMS sender remain under development. An authenticated owner API and client profile/note storage are implemented but not deployed; external SMS delivery remains under development. No production scheduling service has been deployed.

## Local backend

Python 3.12+ is required locally. From the repository root:

```sh
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e './backend[dev]'
backend/.venv/bin/python -m uvicorn scheduling.api:app --app-dir backend --reload
```

Then open `http://127.0.0.1:8000/docs` or request `GET /health`. `GET /v1/businesses/pilot/calendar` requires timezone-aware `start_at` and `end_at` query parameters. `GET /v1/businesses/pilot/availability` accepts `day` and `duration_minutes`, and returns UTC starts under the owner-approved pilot policy. The in-memory calendar contains no customer data and resets on restart. These read endpoints are a local harness, not authenticated production routes.

## Owner command API

`scheduling.owner_auth.create_cognito_owner_app` constructs a separate owner API with a DynamoDB repository and Cognito access-token verification. Its caller must supply the DynamoDB client/table, Cognito issuer, app-client ID, configured owner subject, and business ID. The verifier checks the token signature, issuer, expiry, app client, token type, and exact owner subject; every owner route also checks the business in the path. The hosting layer must expose only this authenticated app for non-local owner access and supply these settings securely. No owner route is mounted by the synthetic `scheduling.api:app` harness.

Owner routes are under `/v1/owner/businesses/{business_id}`. They list pending requests and the calendar, seed/read/edit scheduling policy, create requests, approve or decline an exact request, cancel or edit an appointment, create/move/remove unavailable blocks, and create a confirmed manual appointment. They also list and update client profiles, create/list/delete ordinary client or appointment notes, and set/release a documented legal hold on a note. Every mutation requires `Authorization: Bearer <Cognito access token>`. Scheduling commands and note creation additionally require `Idempotency-Key`; scheduling edits and decisions carry expected versions or calendar revisions. Scheduling-command replays with the same key and body return the original committed result. Client profile writes use expected versions and return a conflict on retry after a successful write; callers should read the current profile before retrying. Note creation uses a stable ID from the key and rejects the same key with different content. Note deletion returns not found on retry after a successful delete. The owner UI and AWS integration are tracked separately in #19 and #23.

The owner cannot mark a phone verified through HTTP. A future verified SMS adapter will call the trusted verification method after validating the inbound provider request. Unverified phones are not returned by verified-phone lookup. Ordinary notes reject recognized entry/access-code phrases. The authenticated owner can set a legal hold with a documented reason through `PATCH .../clients/{client_id}/notes/{note_id}/legal-hold` and release it with `{"reason": null}`. A conditional write prevents a hold update from racing with note deletion. A separate `scheduling.workers.note_retention` handler deletes expired notes; #23 must schedule it before real client data is onboarded. For retention, a confirmed appointment counts as a completed visit once its end time passes, unless cancelled. The handler finds the latest such visit through a strongly consistent appointment scan for the one-business pilot. This favors deletion correctness at low pilot volume; revisit the access pattern if client volume grows. A documented legal hold prevents automatic deletion; release makes an overdue note eligible for the next purge.

Focused checks:

```sh
backend/.venv/bin/python -m pytest backend/tests
backend/.venv/bin/python -m ruff check backend
backend/.venv/bin/python -m mypy backend/scheduling
```
