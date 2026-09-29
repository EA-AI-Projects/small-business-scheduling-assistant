# Small Business Scheduling Assistant

An SMS-first scheduling assistant for a small home-cleaning business. Clients request, reschedule, and cancel visits by text; the owner approves new requests by SMS. A shared scheduling system is the source of truth for availability and appointment status.

## Project documents

- [Product Requirements Document (PRD)](doc/PRD.md)
- [High-Level Design (HLD)](doc/HLD.md)
- [Technical Architecture](doc/ARCHITECTURE.md)
- [Pilot infrastructure plan](doc/PILOT_INFRASTRUCTURE.md)
- [Local model evaluation and secrets](doc/MODEL_EVAL.md)
- [Local scheduling conversation exercise](doc/CONVERSATION.md)
- [Scheduling service contracts](doc/SCHEDULING_CONTRACTS.md)
- [Human–agent team agreement](doc/TEAM_AGREEMENT.md)
- [A2P registration notes](doc/A2P_REGISTRATION.md) and [public SMS policy pages](docs/README.md)

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

## Try the app locally

One backend process and the owner web app share a single fictional, in-memory calendar. Nothing reaches Twilio, AWS, or real phones.

| Process | Start it with | Connects to |
| --- | --- | --- |
| Synthetic owner backend, port 8000 | Terminal A, below | Holds the one in-memory calendar. Optionally calls OpenAI for free-form texts. |
| Owner web app, port 3000 | Terminal B, below | Calls the backend on port 8000. |
| Text simulator page | Served by the backend at `http://127.0.0.1:8000/local/texts` | The same backend and calendar. |

Terminal A, from the repository root. `.env` supplies `OPENAI_API_KEY`. Without that file, `source` prints an error and the backend starts without OpenAI: exact commands still work, and free-form texts get the command prompt.

```sh
(
  set -a; source .env; set +a
  export LOCAL_OWNER_TOKEN="$(openssl rand -hex 16)"; echo "$LOCAL_OWNER_TOKEN"
  backend/.venv/bin/uvicorn scheduling.local_owner:app --app-dir backend --host 127.0.0.1 --port 8000
)
```

Terminal B, from the repository root:

```sh
cd frontend
cp .env.example .env.local   # first time only
npm ci                       # first time only
npm run dev
```

Open `http://127.0.0.1:3000` (owner calendar) and `http://127.0.0.1:8000/local/texts` (text simulator), and paste the printed token into each.

- **Client texts:** choose Avery Example or Blake Sample in the simulator and text `Book YYYY-MM-DD` for a weekday within 14 days that is not a federal holiday, then `Book YYYY-MM-DD HH:MM`. Refresh the owner calendar to see the pending request.
- **Owner decisions:** approve or decline in the owner app, or by texting `Approve REF` as the owner. The simulator shows the notification texts each side would receive, rendered with the production templates.
- **Cancel and reschedule:** client texts `Cancel REF` or `Reschedule REF to YYYY-MM-DD HH:MM`. Rescheduling needs a confirmed visit; the original stays booked until the owner approves the replacement.
- **Unverified senders:** Casey Demo is deliberately unverified. Their texts get no reply, as in production; the simulator shows a note explaining why.

Everything resets when Terminal A stops. Hold expiry and other scheduled workers do not run locally. STOP/HELP keywords are handled by Twilio in production and are not simulated.

## Local backend

Python 3.12+ is required locally. From the repository root:

```sh
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e './backend[dev]'
backend/.venv/bin/python -m uvicorn scheduling.api:app --app-dir backend --reload
```

Then open `http://127.0.0.1:8000/docs` or request `GET /health`. `GET /v1/businesses/pilot/calendar` requires timezone-aware `start_at` and `end_at` query parameters. `GET /v1/businesses/pilot/availability` accepts `day` and `duration_minutes`, and returns UTC starts under the owner-approved pilot policy. The in-memory calendar contains no customer data and resets on restart. These read endpoints are a local harness, not authenticated production routes.

To preview how the model interprets your own fictional texts, use the [interactive model preview](doc/MODEL_EVAL.md) with `--interactive`. It proposes an intent and clarification question but cannot book a visit or send an SMS.

To exercise the booking and approval flow with a fictional client, owner, and in-memory calendar, follow the [local conversation exercise](doc/CONVERSATION.md). It can create and change synthetic appointment state in memory without Twilio, AWS, or live texts.

## Owner command API

`scheduling.owner_auth.create_cognito_owner_app` constructs a separate owner API with a DynamoDB repository and Cognito access-token verification. Its caller must supply the DynamoDB client/table, Cognito issuer, app-client ID, configured owner subject, and business ID. The verifier checks the token signature, issuer, expiry, app client, token type, and exact owner subject; every owner route also checks the business in the path. The hosting layer must expose only this authenticated app for non-local owner access and supply these settings securely. No owner route is mounted by the synthetic `scheduling.api:app` harness.

Owner routes are under `/v1/owner/businesses/{business_id}`. They list pending requests and the calendar, seed/read/edit scheduling policy, create requests, approve or decline an exact request, cancel or edit an appointment, create/move/remove unavailable blocks, and create a confirmed manual appointment. They also list and update client profiles, create/list/delete ordinary client or appointment notes, and set/release a documented legal hold on a note. Every mutation requires `Authorization: Bearer <Cognito access token>`. Scheduling commands and note creation additionally require `Idempotency-Key`; scheduling edits and decisions carry expected versions or calendar revisions. Scheduling-command replays with the same key and body return the original committed result. Client profile writes use expected versions and return a conflict on retry after a successful write; callers should read the current profile before retrying. Note creation uses a stable ID from the key and rejects the same key with different content. Note deletion returns not found on retry after a successful delete. The owner web app is in `frontend/`; AWS deployment is gated by #43.

The owner web app is a separate static Next.js app on AWS Amplify, so the owner API is called cross-origin. `create_cognito_owner_app(..., cors_origins=(...,))` allows only the listed exact origins (`https://host[:port]`, no path, no wildcard) with the owner route methods and the `Authorization`, `Content-Type`, and `Idempotency-Key` headers; credentials are not allowed. The deployed Lambda reads the one allowed origin from `OWNER_APP_ORIGIN`, set from the `OwnerAppOrigin` stack parameter, which also configures the HTTP API CORS policy.

For frontend development, run a synthetic, in-memory owner API on loopback. It seeds the pilot policy and a few fictional records, mounts no SMS routes, accepts only the bearer token in `LOCAL_OWNER_TOKEN` (at least 16 characters), and allows CORS only from `http://localhost:3000` and `http://127.0.0.1:3000`. It is not for deployment.

```sh
LOCAL_OWNER_TOKEN="$(openssl rand -hex 16)" backend/.venv/bin/uvicorn scheduling.local_owner:app --app-dir backend --host 127.0.0.1 --port 8000
```

The owner cannot mark a phone verified through HTTP. Unverified phones are not returned by verified-phone lookup. The signed SMS ingress maps only already verified client phones to scheduling actor context; phone verification enrollment remains a separate trusted step. The owner may record an in-person clear yes and script version at `POST .../clients/{client_id}/sms-consent`; this does not send an enrollment text. Outbound SMS also requires a verified phone, consent for clients, no local opt-out for either recipient, and an explicitly authorized recipient allowlist. Twilio Advanced Opt-Out handles STOP/HELP replies; local STOP remains in force until a newer in-person consent and START are both recorded. The SQS sender requires `SMS_SEND_ENABLED=authorized` and `AUTHORIZED_SMS_RECIPIENTS` before it can send. These settings do not replace business-number/campaign approval or separate test-number authorization. Provider acceptance marks an outbox intent sent; a later signed delivery callback is linked to the outbox ID. Terminal delivery failures are visible through the authenticated owner `GET .../sms-delivery-failures` route for manual follow-up; they do not trigger a blind resend. A provider timeout after acceptance can still cause a duplicate text on retry.

Ordinary notes reject recognized entry/access-code phrases. The authenticated owner can set a legal hold with a documented reason through `PATCH .../clients/{client_id}/notes/{note_id}/legal-hold` and release it with `{"reason": null}`. A conditional write prevents a hold update from racing with note deletion. A separate `scheduling.workers.note_retention` handler deletes expired notes; #23 must schedule it before real client data is onboarded. For retention, a confirmed appointment counts as a completed visit once its end time passes, unless cancelled. The handler finds the latest such visit through a strongly consistent appointment scan for the one-business pilot. This favors deletion correctness at low pilot volume; revisit the access pattern if client volume grows. A documented legal hold prevents automatic deletion; release makes an overdue note eligible for the next purge.

The `scheduling.workers.sms_retention` handler removes SMS bodies 90 days after each sender's last scheduling exchange, including accepted outbound replies, while retaining receipt IDs for deduplication. It also deletes minimal consent and immutable STOP evidence four calendar years after the phone's last provider-verified inbound client/owner text (including STOP) or accepted outbound program text; consent with no text uses its agreement date. An active STOP retains a minimal phone-number suppression marker beyond historical evidence expiry until a fresh consent followed by START clears it; evidence deletion cannot silently enable delivery. Existing STOP records without a marker are treated as active, and the purge creates a marker atomically before deleting such a record. Existing threads without the new evidence clock fall back to their last exchange timestamp. `DynamoSmsIngressStore.set_evidence_legal_hold` is a trusted administrative method for a documented hold on a specific evidence record. #23 must schedule the retention handler (for example, daily through EventBridge), wire a separately authorized hold-management path, and monitor failures before real SMS data is onboarded; neither handler deploys itself.

### Owner web app

The owner calendar is a static Next.js + React + TypeScript app in [`frontend/`](frontend/README.md), built for AWS Amplify Hosting. It signs in through the [Cognito authorization-code PKCE flow](https://docs.aws.amazon.com/cognito/latest/developerguide/using-pkce-in-authorization-code.html), keeps the access token in page memory only, and calls the owner API cross-origin. The owner API allows CORS only from the stack's `OwnerAppOrigin`. The owner token verifier still checks the exact configured owner subject and business ID; no browser-supplied role or business ID grants access.

The app provides day/week schedule and pending approvals in the business timezone, appointment edits/cancellation, unavailable blocks, date exceptions, client profiles, and separate client/visit notes. Local date-time inputs are resolved by the authenticated API, which rejects ambiguous or nonexistent daylight-saving times. A conflicting edit refreshes current state and states that nothing was saved. The app bundle and its public build settings contain no customer data or credentials. For local development against the synthetic `scheduling.local_owner` API, see [`frontend/README.md`](frontend/README.md). Creating the Amplify app is a deployment step gated by #43.

Focused checks (GitHub Actions `CI` also runs these, plus the DynamoDB Local race tests and `cfn-lint template.yaml`, on every pull request and push to `main`):

```sh
backend/.venv/bin/python -m pytest backend/tests
backend/.venv/bin/python -m ruff check backend
backend/.venv/bin/python -m mypy backend/scheduling
```
