# Smart Scheduling Assistant

An SMS-first scheduling assistant for a small home-cleaning business. Clients request, reschedule, and cancel visits by text; the owner approves new requests by SMS. A shared scheduling system is the source of truth for availability and appointment status.

The product name is **Smart Scheduling Assistant**. The repository name and deployment identifiers remain stable. The approved visual identity is a black-and-white, nonhuman face with bookworm glasses and freckles. The [primary logo](frontend/public/brand/mark.svg) is the owner app's brand mark; the [simplified favicon](frontend/public/favicon.svg) is for browser tabs and small icon surfaces. See the [product identity decision](doc/PRD.md#1-summary).

## Project documents

- [Product Requirements Document (PRD)](doc/PRD.md)
- [High-Level Design (HLD)](doc/HLD.md)
- [Technical Architecture](doc/ARCHITECTURE.md)
- [Pilot infrastructure plan](doc/PILOT_INFRASTRUCTURE.md)
- [Synthetic dev stack: cost, resources, and rollback packet](doc/DEV_STACK_PLAN.md)
- [Local model evaluation and secrets](doc/MODEL_EVAL.md)
- [SMS conversation flow and local exercise](doc/CONVERSATION.md)
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
Install the backend dependencies first using the commands in [Local backend](#local-backend).
The text simulator needs only Terminal A; start Terminal B if you also want to use the owner web app.

| Process | Start it with | Connects to |
| --- | --- | --- |
| Synthetic owner backend, port 8000 | Terminal A, below | Holds the one in-memory calendar. Calls OpenAI to interpret plain-language texts when a key is set. |
| Owner web app, port 3000 | Terminal B, below | Calls the backend on port 8000. |
| Text simulator page | Served by the backend at `http://127.0.0.1:8000/local/texts` | The same backend and calendar. |

Terminal A, from the repository root. If the ignored `.env` file supplies `OPENAI_API_KEY`, plain-language texts use OpenAI. Without a key, exact commands and replies to an offer still work; other texts receive a clarification and a note explaining the missing key.

```sh
(
  if [ -f .env ]; then set -a; source .env; set +a; fi
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

Open `http://127.0.0.1:8000/local/texts` and paste the token printed by Terminal A. If you started Terminal B, open `http://127.0.0.1:3000` for the owner calendar and use the same token there. Both pages share the same in-memory calendar.

- **Client texts:** choose Avery Example or Blake Sample and ask in plain language, for example "Do you have availability for tomorrow?". The reply offers 3–5 open times and writes nothing. Answer with one of them ("10 works", "option 2", or "yes" when one time was offered) within 30 minutes to create a pending request. Refresh the owner calendar to see it. The model sees that client's recent inbound texts and displayed replies under the production 24-hour history bounds. See the [conversation flow](doc/CONVERSATION.md) for what counts as a pick.
- **Owner decisions:** text `yes` or `decline` as the owner when exactly one request is pending; the seeded data starts with one. With several pending, the reply lists them and asks for `Approve REF` or `Decline REF`. You can also decide in the owner app. The simulator shows the notification texts each side would receive, rendered with the production templates. After a text changes the calendar, the assistant's own reply appears as a grey "Not texted" note, because production sends only the notifications for a change.
- **Cancel and reschedule:** as a client, text "I can't make Thursday" and confirm with `yes`, or "Can I move my Thursday visit to Friday?" and pick a replacement time. Rescheduling needs a confirmed visit; the original stays booked until the owner approves the replacement.
- **Exact commands** still work: `Book YYYY-MM-DD`, `Book YYYY-MM-DD HH:MM`, `Cancel REF`, `Reschedule REF to YYYY-MM-DD HH:MM`, `Approve REF`, and `Decline REF`.
- **Unverified senders:** Casey Demo is deliberately unverified. Their texts get no reply, as in production; the simulator shows a note explaining why.

Everything resets when Terminal A stops. Hold expiry and other scheduled workers do not run locally. STOP/HELP keywords are handled by Twilio in production and are not simulated.

### Replay a conversation as a manual test

The [sample scenario](backend/evals/scenarios/booking_request.json) sends several fictional texts through the same simulator logic, starting with a fresh calendar at its fixed `start_at` time. Copy and edit the JSON file to reproduce an issue. Each step gives a `party` (`owner` or `client-1` to `client-3`) and `body`; `expect` can check outbound text with `out_contains`, pending requests with `pending_for`, and event counts with `calendar_statuses`. Party names are case insensitive. Use `{{pending_ref:client-1}}` in an owner text to insert that client's one current pending request reference; a bare `approve` cannot choose between multiple pending requests. The runner prints replies, notifications, notes, and calendar counts after every step.

To run one scenario manually with OpenAI, from the repository root:

```sh
(
  set -a
  source .env
  set +a
  backend/.venv/bin/python backend/evals/conversation_scenarios.py \
    backend/evals/scenarios/booking_request.json --live
)
```

This requires `OPENAI_API_KEY` in the ignored `.env` file and the explicit `--live` flag. It is separate from pytest and CI, makes billable OpenAI calls, and sends no SMS or AWS requests. A failure prints the actual exchange so you can inspect where it diverged. See [manual multi-step conversation scenarios](doc/MODEL_EVAL.md#manual-multi-step-conversation-scenarios) for the full scenario format. Use fictional data only.

## Deploy to dev

The long-lived synthetic `dev` environment (#43) has helper scripts in [`scripts/dev/`](scripts/dev). Sign in with `aws sso login --profile scheduling-dev-admin` (the scripts use the `scheduling-dev-deployer` profile, which assumes its role through that sign-in; `aws sso login` on the deployer profile itself fails), then run from the repository root. Each script targets only account `214965372605`, region `us-west-1`, and accepts `--dry-run` (prints commands, calls no AWS beyond the identity check).

```sh
scripts/dev/deploy-backend.sh     # sam build, change set, readable summary, schedule-state guard, y/N, execute, smoke test
scripts/dev/deploy-frontend.sh    # build with stack outputs, keep the zip in S3, Amplify deploy, header check
scripts/dev/status.sh             # read-only: stack, schedules, deployment, alarms, budget
scripts/dev/schedules.sh enable|disable <LogicalId>   # full deploy of this checkout (prints commit; --allow-dirty for a dirty tree) that sets the schedule's parameter; never outbox dispatch. Emergency stop: disable-rule, DEV_STACK_PLAN section 3.1
```

These scripts deploy the **local checkout**, not what is on GitHub. Each prints the commit it deploys. Two guards apply: a dirty working tree is refused unless you pass `--allow-dirty` (then a warning says the uncommitted changes will ship; `--dry-run` and `deploy-backend.sh --smoke-only` are never blocked by the dirty check), and a warning, never a block, appears when `HEAD` is on no remote branch ("commit <sha> is not on GitHub yet"; a quiet `git fetch` runs first, and if it fails the local remote-tracking refs are used). `schedules.sh` passes `--allow-dirty` on to `deploy-backend.sh`.

No parameter value is read into the repository or printed. Details and the IAM grant are in [Deploy to dev with scripts](doc/PILOT_INFRASTRUCTURE.md#deploy-to-dev-with-scripts).

## Local backend

Python 3.12+ is required locally. From the repository root:

```sh
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e './backend[dev]'
backend/.venv/bin/python -m uvicorn scheduling.api:app --app-dir backend --reload
```

Then open `http://127.0.0.1:8000/docs` or request `GET /health`. `GET /v1/businesses/pilot/calendar` requires timezone-aware `start_at` and `end_at` query parameters. `GET /v1/businesses/pilot/availability` accepts `day` and `duration_minutes`, and returns UTC starts under the owner-approved pilot policy. The in-memory calendar contains no customer data and resets on restart. These read endpoints are a local harness, not authenticated production routes.

To preview how the model interprets your own fictional texts, use the [interactive model preview](doc/MODEL_EVAL.md) with `--interactive`. It proposes an intent and clarification question but cannot book a visit or send an SMS.

To exercise the plain-language booking, cancellation, rescheduling, and approval flow with a fictional client, owner, and in-memory calendar, follow the [conversation flow and local exercise](doc/CONVERSATION.md). It can create and change synthetic appointment state in memory without Twilio, AWS, or live texts.

## Owner command API

### Account provisioning and recovery

The approved MVP account scope is recorded in [the PRD](doc/PRD.md#60-owner-and-client-accounts-224). The current deployed design and local harness support the owner-only path described below; #226–#229 implement the approved owner and client account flow. This is an operator process, not an administrator portal:

1. For an owner, an administrator records the invitation or approval and the exact business in a private operator record. They create the Cognito owner through the Cognito administrator console or approve an existing account, wait for the account email to be verified and the account to become confirmed, then run `python -m scheduling.tools.provision_approved_owner --table TABLE --pool POOL --email EMAIL --business BUSINESS --actor OPERATOR --approval-reference REFERENCE` with the backend package on `PYTHONPATH`. The command checks the Cognito account and writes an auditable business link. It does not create a new user or send email; the administrator controls that step. The current pilot also keeps the exact `OwnerSub`/`BusinessId` gate. Never run this against live AWS without separate deployment authorization.
2. For a client, the owner first creates the profile, then calls `POST /v1/owner/businesses/{business_id}/clients/{client_id}/account-invitation` with the client's email. The service creates a pending link, and Cognito emails the account invitation with the `/client/` sign-in address. It expires 24 hours after the owner sends it. For a newly created Cognito account, the directory sets `email_verified=true` at creation, then sends a temporary password only to the invited address; receiving that password and signing in proves control of the address before activation. An existing Cognito account must already have the same verified email and remain enabled; the invitation workflow never changes verification on existing accounts. The app client cannot write the account email after sign-in. The signed-in client activates at `POST /v1/account/invitations/activate` with a Cognito ID token; the server finds the pending link from the verified subject. Its signature, audience, token type, subject, verified email, invite expiry, and existing profile must all match. An uninvited address or a caller who only knows profile details cannot claim it. This API work sends no email until a separately authorized deployment and owner action.
3. The administrator handles a lost owner account or changed identity by verifying the owner approval again and restoring or replacing only its authorized business link. The owner handles a client's changed address or identity by checking the existing profile and issuing a new invitation for it. Cognito email recovery alone restores account sign-in; it must not create or transfer a business or client link. A missing, revoked, or mismatched link results in denied access. #228 enforces these boundaries on owner and client APIs; #229 presents the recovery and denied-access paths.

Account email verification proves control of an email address for sign-in. It does not verify the client's phone, grant permission for SMS, or clear an opt-out. The separate in-person phone verification and consent process below still applies. No administrator portal or client scheduling pages are included in this MVP.

`scheduling.linked_auth.create_cognito_linked_app` constructs the deployed owner and client API with a DynamoDB repository and Cognito access-token verification. Its caller supplies the DynamoDB client/table, Cognito issuer, and app-client ID. The verifier checks the token signature, issuer, expiry, app client, and token type, then resolves the active server-side identity link. Every owner route requires an owner link for the business in the path. `GET /v1/client/session` requires a client link and returns only that link's business and client IDs. The hosting layer must expose only this authenticated app for non-local access and supply these settings securely. No owner or client route is mounted by the synthetic `scheduling.api:app` harness.

Inviting the same profile and email while an invitation remains valid returns its original expiry without another email. To change an address or replace an identity, the owner first calls `DELETE .../account-invitation`; this revokes the old link, including one that was already activated, and records the operator and reason. The owner then issues a new invitation for the existing profile. An expired invitation is revoked before replacement. A deleted profile cannot be invited or activated; deletion removes its invitation and link records. If Cognito sends an email but the API cannot record delivery, a retry can resend it; only the current invite and verified identity can activate. Existing unrelated Cognito accounts do not gain access from sign-in alone.

Owner routes are under `/v1/owner/businesses/{business_id}`. They list pending requests and the calendar, seed/read/edit scheduling policy, create requests, approve or decline an exact request, cancel or edit an appointment, create/move/remove unavailable blocks, and create a confirmed manual appointment. They also list and update client profiles, create/list/delete ordinary client or appointment notes, and set/release a documented legal hold on a note. Every mutation requires `Authorization: Bearer <Cognito access token>`. Scheduling commands and note creation additionally require `Idempotency-Key`; scheduling edits and decisions carry expected versions or calendar revisions. Scheduling-command replays with the same key and body return the original committed result. Client profile writes use expected versions and return a conflict on retry after a successful write; callers should read the current profile before retrying. Note creation uses a stable ID from the key and rejects the same key with different content. Note deletion returns not found on retry after a successful delete. The owner web app is in `frontend/`; AWS deployment is gated by #43.

The owner web app is a separate static Next.js app on AWS Amplify, so the API is called cross-origin. `create_cognito_linked_app(..., cors_origins=(...,))` allows only the listed exact origins (`https://host[:port]`, no path, no wildcard) with the route methods and the `Authorization`, `Content-Type`, and `Idempotency-Key` headers; credentials are not allowed. The deployed Lambda reads the one allowed origin from `OWNER_APP_ORIGIN`, set from the `OwnerAppOrigin` stack parameter, which also configures the HTTP API CORS policy.

For frontend development, run a synthetic, in-memory owner API on loopback. It seeds the pilot policy and a few fictional records, mounts no SMS routes, accepts only the bearer token in `LOCAL_OWNER_TOKEN` (at least 16 characters), and allows CORS only from `http://localhost:3000` and `http://127.0.0.1:3000`. It is not for deployment.

```sh
LOCAL_OWNER_TOKEN="$(openssl rand -hex 16)" backend/.venv/bin/uvicorn scheduling.local_owner:app --app-dir backend --host 127.0.0.1 --port 8000
```

There is no standalone owner route that marks a phone verified, and a profile edit that changes the phone clears `phone_verified_at`. The owner and client meet in person, the owner reads the number back, and the owner records the client's clear yes and script version at `POST .../clients/{client_id}/sms-consent`; that one atomic write stores the consent records and marks the profile phone verified (owner decision on #91, replacing the earlier rule that the owner cannot verify a phone). If the profile version or phone changed meanwhile, nothing is recorded (409). Nothing proves the client holds the phone; a new number needs fresh consent, which is its re-verification. For a client's first enrollment (phone never verified, no earlier consent record, no `welcome#<client_id>` outbox record) the same write also queues one welcome text with the consent-page wording; re-recording consent, including after STOP/START, queues none, and the sender still enforces consent, opt-out, verified phone and the live-send gate at delivery. Unverified phones are not returned by verified-phone lookup, and the signed SMS ingress maps only verified client phones to scheduling actor context. Outbound SMS also requires a verified phone, consent for clients, no local opt-out for either recipient. Owner onboarding (recorded in-person consent, which also verifies the phone) is the only recipient gate; there is no deploy-time recipient list, so adding a tester needs no deploy. A number in the fictional range 555-0100 to 555-0199 (any area code) is always refused with `FICTIONAL_NUMBER`. Twilio answers STOP/HELP itself (Advanced Opt-Out is enabled in `dev` with CANCEL removed from its opt-out keywords, so a bare "Cancel" is an ordinary scheduling message; revisit before the pilot, see `doc/DEV_SMS_PLAN.md`); local STOP remains in force until a newer in-person consent and START are both recorded. The SQS sender requires `SMS_SEND_ENABLED=authorized` before it can send. These settings do not replace business-number/campaign approval or owner onboarding (in-person consent and verification, #91). Provider acceptance marks an outbox intent sent; a later signed delivery callback is linked to the outbox ID. Terminal delivery failures are visible through the authenticated owner `GET .../sms-delivery-failures` route for manual follow-up; they do not trigger a blind resend. A provider timeout after acceptance can still cause a duplicate text on retry.

Ordinary notes reject recognized entry/access-code phrases. The authenticated owner can set a legal hold with a documented reason through `PATCH .../clients/{client_id}/notes/{note_id}/legal-hold` and release it with `{"reason": null}`. A conditional write prevents a hold update from racing with note deletion. A separate `scheduling.workers.note_retention` handler deletes expired notes; #23 must schedule it before real client data is onboarded. For retention, a confirmed appointment counts as a completed visit once its end time passes, unless cancelled. The handler finds the latest such visit through a strongly consistent appointment scan for the one-business pilot. This favors deletion correctness at low pilot volume; revisit the access pattern if client volume grows. A documented legal hold prevents automatic deletion; release makes an overdue note eligible for the next purge.

The `scheduling.workers.sms_retention` handler removes SMS bodies 90 days after each sender's last scheduling exchange, including accepted outbound replies (except in the owner's own thread, where each text is removed 90 days after that message, #201), while retaining receipt IDs for deduplication. It also deletes minimal consent and immutable STOP evidence four calendar years after the phone's last provider-verified inbound client/owner text (including STOP) or accepted outbound program text; consent with no text uses its agreement date. An active STOP retains a minimal phone-number suppression marker beyond historical evidence expiry until a fresh consent followed by START clears it; evidence deletion cannot silently enable delivery. Existing STOP records without a marker are treated as active, and the purge creates a marker atomically before deleting such a record. Existing threads without the new evidence clock fall back to their last exchange timestamp. `DynamoSmsIngressStore.set_evidence_legal_hold` is a trusted administrative method for a documented hold on a specific evidence record. #23 must schedule the retention handler (for example, daily through EventBridge), wire a separately authorized hold-management path, and monitor failures before real SMS data is onboarded; neither handler deploys itself.

### Owner web app

The owner calendar is a static Next.js + React + TypeScript app in [`frontend/`](frontend/README.md), built for AWS Amplify Hosting. It signs in through the [Cognito authorization-code PKCE flow](https://docs.aws.amazon.com/cognito/latest/developerguide/using-pkce-in-authorization-code.html), keeps the access token in page memory only, and calls the owner API cross-origin. The owner API allows CORS only from the stack's `OwnerAppOrigin`. The owner token verifier still checks the exact configured owner subject and business ID; no browser-supplied role or business ID grants access.

The app provides day/week schedule and pending approvals in the business timezone, appointment edits/cancellation, unavailable blocks, date exceptions, client profiles, and separate client/visit notes. Local date-time inputs are resolved by the authenticated API, which rejects ambiguous or nonexistent daylight-saving times. A conflicting edit refreshes current state and states that nothing was saved. The app bundle and its public build settings contain no customer data or credentials. For local development against the synthetic `scheduling.local_owner` API, see [`frontend/README.md`](frontend/README.md). Creating the Amplify app is a deployment step gated by #43.

Focused checks (GitHub Actions `CI` also runs these, plus the DynamoDB Local race tests, `cfn-lint` on both templates, and `shellcheck` on `scripts/dev`, on every pull request and push to `main`):

```sh
backend/.venv/bin/python -m pytest backend/tests
backend/.venv/bin/python -m ruff check backend
backend/.venv/bin/python -m mypy backend/scheduling
```
