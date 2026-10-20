# Synthetic scheduling-message model evaluation

**Scope:** Current synthetic harness instructions, reviewed 2026-10-14. The repository's selected model is configured in `scheduling.adapters.openai_messages`; the original selection/evaluation provenance is issue #24. Running these harnesses makes billable model calls and requires the applicable explicit authorization.

Use a project-scoped API key for local testing. The repository ignores `.env` and `.env.*`, while `.env.example` contains names only. Keep the real `.env` at the repository root, restrict it to your user (`chmod 600 .env`), and never paste its contents into chat, GitHub, logs, or a test result. The script reads `OPENAI_API_KEY` from its process environment; it does not load `.env` itself. From the repository root:

```sh
(
set -a
source .env
set +a
cd backend
.venv/bin/python -m evals.scheduling_messages
)
```

To try your own **fictional** messages, use the same subshell setup and run
`.venv/bin/python -m evals.scheduling_messages --interactive` from `backend` instead. Type
`/quit` to exit. The client preview uses two invented visit references
(`a101a101`, `b202b202`) and prints the model's proposed interpretation, including
any resolved dates and clarification question. It does not exercise the #297
owner tool loop. It does not send an SMS, change the calendar, or apply
the backend permission and availability checks.
Do not enter real names, phone numbers, addresses, access codes, or customer
messages. The preview rejects recognizable
phone numbers and access-code phrases before an API call, but this filter cannot
detect every kind of private information.

### Manual multi-step conversation scenarios

For an issue that needs a whole conversation, copy and edit
[`booking_request.json`](../backend/evals/scenarios/booking_request.json). Each run starts
with a fresh fictional local calendar and the scenario's fixed `start_at` time.
Each step sends one text as `owner` or `client-1` to `client-3`; party names are
case insensitive. To name a request created during the run, use
`"Approve {{pending_ref:client-1}}"` in an owner step. The runner replaces that
placeholder with the one pending request reference for that client; it fails
before sending if there are zero or several. Optional
`advance_minutes` moves the clock before that text. Optional `expect` checks
outbound text fragments with `out_contains` and active request counts with
`pending_for`, and calendar event counts with `calendar_statuses`. The runner
prints every displayed reply, notification, note, pending-request count, and
calendar status count, then exits with a failure code if a check fails.
Use only fictional text; the local simulator rejects recognizable phone numbers
and access codes, but cannot detect every private detail.

From the repository root, manually run one scenario with the production OpenAI
interpreter and the local simulator:

```sh
(
  set -a
  source .env
  set +a
  backend/.venv/bin/python backend/evals/conversation_scenarios.py \
    backend/evals/scenarios/booking_request.json --live
)
```

This script is outside pytest and CI. It requires both `--live` and
`OPENAI_API_KEY`, so an ordinary test run never calls OpenAI. A live run uses
OpenAI API calls and may incur charges, but sends no SMS and uses no AWS service.
Checks should focus on calendar outcomes rather than exact model wording, since
the model can phrase valid replies differently between runs. The scenario file
allows at most 20 steps per run; use a short focused reproduction for each issue.

For local tests, a password manager can supply the value when creating `.env`; no secret value or vault item reference belongs in this repository. Add future local-only variables to the ignored file as their test harnesses need them. The deployed Twilio integration uses an environment-scoped SSM SecureString and has separate SMS authorization gates; a local `.env` does not configure or authorize live messaging.

The script sends synthetic client texts (from #24, #60, and #241) with no real people, phones, addresses, or access codes, using the intent-proposal instructions/schema from `scheduling.adapters.openai_messages`. This tests the initial interpretation and legacy deterministic/fallback path, not the current client tool loop's tool selection or final replies; open-prompt answers can enter that loop directly. "Today" is fixed at Monday 2026-09-28. Since #60, the model resolves relative dates instead of refusing them, so the checks are:

- An impossible date (February 30) must clarify with every action field null.
- "Tomorrow" must resolve to 2026-09-29. "Next Friday afternoon" may clarify, or resolve to one Friday (Oct 2 or Oct 9) within 12:00–17:00. "Tuesday around 3" may clarify, or resolve to 2026-09-29 within 14:00–16:00.
- "Cancel my appointment" with two visits must name neither a reference nor a day. "I can't make Thursday" must propose `cancel` for 2026-10-01 without a reference.

The client model sees one `propose_message` function. The script provides no scheduling write tool and does not call the calendar service. A passing run does not replace backend validation.

The response is a JSON summary of the model's proposals. A failed case returns a nonzero exit code. Record the summary, model ID and commit in the evaluation issue after an authorized live run; do not store the API key. Evaluate the current tool loop separately before claiming conversation coverage.

### Client calendar questions (#241)

Since #241 the model, not keyword rules, decides whether a client text is a calendar question (owner decision, 2026-10-05). The script adds eleven client cases. Follow-up cases send the same "Last calendar answer" line the service sends:
- The three texts from the #241 screenshot must be `calendar_question`. The second must ask only about confirmed visits, and the third is a summary.
- "How many confirmed visits next week?" must be a confirmed-only count for 2026-10-05 to 2026-10-11.
- A Spanish question and a Spanish follow-up ("¿Y la próxima semana?") must be calendar questions with the right range.
- "Just the confirmed ones" after an answer must change only the statuses.
- "What times are open Friday?" and "Can I get a cleaning Friday instead?" must stay availability requests.
- "Booking for Friday?" must clarify.

### Owner scheduling preferences (#380)

`backend/evals/scheduling_preferences.py` holds sixteen synthetic cases for the interpreter behind Review changes: two supported inputs (vague "mornings" must become a bounded range, an explicit afternoon range), vague text, a contradiction, avoidance, unsupported route grouping, client-specific guidance, mixed business-wide and client guidance, two named clients, duplicate profile names (must surface as ambiguity, resolved by code against synthetic profiles), an unknown client, a same-scope contradiction, a group of clients (unsupported), and two attempts to override booking constraints (hours/buffers, owner approval). `python -m evals.scheduling_preferences` lists them and makes no call; `--live` with `OPENAI_API_KEY` runs them through the production interpreter and deterministic validator and is billable, so it needs separate authorization. The pytest suite uses a scripted model and proves permissions, persistence and effects only, not interpretation quality. No live run has been made for #380.

### Client-specific SMS offers (#383)

Scripted verification and real-model evaluation are separate. `backend/tests/test_sms_client_preferences.py` uses a scripted model for the tool choices and replies and proves backend behavior only: the verified sender's client id selects the rules, precedence matches the portal, tool arguments cannot supply an identity, and acceptance, revalidation and replay are unchanged. It says nothing about how a real model words an offer or interprets owner text. The real-model cases that exist for this feature are the sixteen `backend/evals/scheduling_preferences.py` interpretation cases listed above (including client-specific, mixed, two-client, duplicate-name and unknown-client guidance) and the existing client tool-loop and conversation scenarios in `backend/evals`. No case was added for #383 and none was run live; a live run is billable and needs separate authorization.

## Evaluation evidence

Dated results and removed-path evaluations are in [MODEL_EVAL_RESULTS.md](MODEL_EVAL_RESULTS.md). Record a new run's commit, model, prompt/tool version, date, cases and boundary; do not replace current behavior documentation with a score or infer deployed proof from a synthetic run.
