# Smart Scheduling Assistant

An SMS-first scheduling assistant for a small home-cleaning business. Clients request, reschedule, and cancel visits by text; the owner approves new requests by SMS. A shared scheduling system is the source of truth for availability and appointment status.

The product name is **Smart Scheduling Assistant**. The repository name and deployment identifiers remain stable. The approved visual identity is a black-and-white, nonhuman face with bookworm glasses and freckles. The [primary logo](frontend/public/brand/mark.svg) is the owner app's brand mark; the [simplified favicon](frontend/public/favicon.svg) is for browser tabs and small icon surfaces. See the [product identity decision](doc/PRD.md#1-summary).

## Project documents

Start with the [documentation index](doc/README.md) for each document's purpose and status. Current requirements, design and architecture are separate from [decision history](doc/DECISION_HISTORY.md), operational runbooks and evaluation evidence. Public SMS policy pages live under `docs/`.

## Current capabilities

- Flexible, one-off appointment requests (recurring schedules deferred).
- Owner approval required before new appointments are confirmed.
- Configurable pending-request hold, defaulting to 24 hours.
- Booking horizon of 14 days.
- Monday–Friday, 8:00 a.m.–5:00 p.m. in `America/Los_Angeles`; observed US federal holidays are closed, with owner-editable exceptions.
- One saved service default duration, capped by policy (pilot baseline: 3 hours), with an owner override per appointment.
- Configurable 15-minute start-time increments and one crew for the pilot.
- Configurable 30-minute travel buffer between visits, with none before the first or after the last.
- SMS-first owner and client workflows, an owner calendar/admin app, and client Calendar and Appointments pages.
- Free client cancellations; cancelled time becomes available for future requests.
- Optional confirmed-visit SMS reminders with per-client timing and enrollment through owner profiles, the client portal, or text. Scheduled delivery defaults disabled.

## Status

The repository implements authenticated owner/client APIs, the static app, transactional scheduling, signed SMS ingress, bounded model tools, outbox delivery, invitations, retention and deployment templates/scripts. Default-duration booking requires a saved scheduling policy and one-service business profile. Local harnesses use synthetic in-memory data.

Dev deployment checkpoints have been recorded; this README does not assert current AWS resources, enabled gates or live messaging status. Read those from AWS and private operator records. Deployment, real-data onboarding, billable model calls and live SMS need the applicable explicit authorization.

## Try the app locally

One backend process and the owner web app share a single fictional, in-memory calendar. Nothing reaches Twilio, AWS, or real phones.
Install the backend dependencies first using the commands in [Local backend](#local-backend).
The text simulator needs only Terminal A; start Terminal B if you also want to use the owner web app.

| Process | Start it with | Connects to |
| --- | --- | --- |
| Synthetic owner backend, port 8000 | Terminal A, below | Holds the one in-memory calendar. Calls OpenAI to interpret plain-language texts when a key is set. |
| Owner web app and client page, port 3000 | Terminal B, below | Call the backend on port 8000. The owner app is at `/`, the fictional client page at `/client/`. |
| Text simulator page | Served by the backend at `http://127.0.0.1:8000/local/texts` | The same backend and calendar. |

Terminal A starts the backend and prints the owner token first, then one sign-in token for each verified fictional client (Avery Example and Blake Sample). Casey Demo is unverified and has no client sign-in. The client tokens are random, exist only in that process, and are accepted only by the client routes; the owner token works only for owner routes and the text simulator.

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

Open `http://127.0.0.1:8000/local/texts` and paste the owner token printed by Terminal A. If you started Terminal B, open `http://127.0.0.1:3000` for the owner calendar and use the same owner token there. All pages share the same in-memory calendar.

#### Try the client portal as a fictional client

1. Terminal B must run in local mode (`.env.example` already sets `NEXT_PUBLIC_AUTH_MODE=local`, `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000`; local mode is refused for any non-loopback API URL).
2. Open `http://127.0.0.1:3000/client/` and paste one client token from Terminal A, for example the line `Avery Example (client-1): <token>`. The token stays in page memory only; reloading signs you out.
3. Pick a day to see the open times offered for that client's visit length, choose one, and send the request. It is pending and holds nothing as booked until the owner approves.
4. Open `http://127.0.0.1:3000` in another tab, sign in with the owner token, and approve or decline the request under pending requests. Or, in the text simulator, text as the owner `Approve REF` (or `yes` when it is the only pending request). Reload `/client/` (sign in again) to see the status.
5. As the client, cancel a visit, or move a confirmed one (the original stays confirmed until the owner approves the replacement). Avery starts with one confirmed visit and Blake with one pending request.

Known limit: revoking a client in the local owner app does not stop that client's local token; the local verifier always returns an active link. Everything resets when Terminal A stops.

Use a private window or sign out to try the other client; each token reaches only its own client's bookings. The client page and the text simulator use the same backend rules.

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

See [AWS infrastructure and operations](doc/PILOT_INFRASTRUCTURE.md#deploy-to-dev-with-scripts) for `scripts/dev/`, identity/region guards, local-checkout deployment and manual Amplify uploads. Account details and physical IDs stay in private operator configuration. Review [rollback and teardown](doc/DEV_STACK_PLAN.md) before an authorized deployment.

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

## Account provisioning and recovery

Owner administrative approval, client invitations, identity-link activation/revocation and recovery are documented in [ACCOUNTS.md](doc/ACCOUNTS.md). Email verification does not grant SMS consent or phone verification. Current API routes and schemas live in the code and generated owner OpenAPI contract; behavioral guarantees are in [SCHEDULING_CONTRACTS.md](doc/SCHEDULING_CONTRACTS.md).

## Owner web app

See [frontend/README.md](frontend/README.md) for local setup, public build configuration and static export checks. The app supports owner calendar/requests/clients/Settings and separate client Calendar/Appointments pages. Scheduling rules remain in the shared backend.

## Checks

CI runs backend/frontend checks, generated-contract comparisons, DynamoDB Local races, template lint and shellcheck. Local backend checks:

```sh
backend/.venv/bin/python -m pytest backend/tests
backend/.venv/bin/python -m ruff check backend
backend/.venv/bin/python -m mypy backend/scheduling
```

Frontend checks and limits are in [frontend/README.md](frontend/README.md); deployed proof is separate in [AWS_VALIDATION.md](doc/AWS_VALIDATION.md).
