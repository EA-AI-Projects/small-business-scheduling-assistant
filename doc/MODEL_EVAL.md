# Synthetic scheduling-message model evaluation

Issue [#24](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/24) starts with the owner-selected OpenAI `gpt-6-luna` candidate. OpenAI's [model page](https://developers.openai.com/api/docs/models/gpt-6-luna) confirms the ID and Responses function calling; the [function-calling guide](https://developers.openai.com/api/docs/guides/function-calling) describes the tool schema used here.

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

The script sends synthetic client texts (from #24, #60, and #241) with no real people, phones, addresses, or access codes, using the production client instructions, tool schema, and model input from `scheduling.adapters.openai_messages`. "Today" is fixed at Monday 2026-09-28. Since #60, the model resolves relative dates instead of refusing them, so the checks are:

- An impossible date (February 30) must clarify with every action field null.
- "Tomorrow" must resolve to 2026-09-29. "Next Friday afternoon" may clarify, or resolve to one Friday (Oct 2 or Oct 9) within 12:00–17:00. "Tuesday around 3" may clarify, or resolve to 2026-09-29 within 14:00–16:00.
- "Cancel my appointment" with two visits must name neither a reference nor a day. "I can't make Thursday" must propose `cancel` for 2026-10-01 without a reference.

The client model sees one `propose_message` function. The script provides no scheduling write tool and does not call the calendar service. A passing run does not replace backend validation.

The response is a JSON summary of the model's proposals. A failed clarification case returns a nonzero exit code and prevents selecting the candidate as the pilot model. Store the summary and model ID in #24 after the live synthetic run; do not store the API key.

### Client calendar questions (#241)

Since #241 the model, not keyword rules, decides whether a client text is a calendar question (owner decision, 2026-10-05). The script adds eleven client cases. Follow-up cases send the same "Last calendar answer" line the service sends:
- The three texts from the #241 screenshot must be `calendar_question`. The second must ask only about confirmed visits, and the third is a summary.
- "How many confirmed visits next week?" must be a confirmed-only count for 2026-10-05 to 2026-10-11.
- A Spanish question and a Spanish follow-up ("¿Y la próxima semana?") must be calendar questions with the right range.
- "Just the confirmed ones" after an answer must change only the statuses.
- "What times are open Friday?" and "Can I get a cleaning Friday instead?" must stay availability requests.
- "Booking for Friday?" must clarify.

On 2026-10-05 the owner ran all 20 cases against `gpt-6-luna` with the #245 instructions: **18/20 passed** (summary posted on #246).
- All three screenshot texts, both Spanish cases, the count, the status-only follow-up, and "Show me all my upcoming visits" (`range_scope: all_upcoming`) passed.
- Missed: "Can I get a cleaning Friday instead?" during a calendar conversation came back `clarify_booking` (the client would get one extra check-or-request question before the offer).
- Missed: "Booking for Friday?" came back `availability` (the client would get Friday's open times instead of the check-or-request question #240 requires).
- Neither miss can write. #246 adds instruction examples for both kinds of text, worded differently from the eval texts so the two cases stay held out.
- On 2026-10-06, with the #246 instructions, the first rerun passed 19/20: "Can I get a cleaning Friday instead?" was now a request, but "Booking for Friday?" was still `availability`. With an explicit exception added to the availability rule (a short question that only names a booking and a day is `clarify_booking`), two more runs passed **20/20** each. Per-case results are posted on #246. Three runs of a nondeterministic model are evidence, not a guarantee; every reading remains a proposal that writes nothing.

## Current result

### Client reply drafts (#283)

`backend/evals/scheduling_messages.py --drafts` defines five synthetic result-and-transcript cases: an invitation followed by an offered time, pending request, taken slot, confirmed cancellation, and kept visit. It uses the production draft adapter and backend validator. Each additional live run needs separate authorization; offline scripted conversation and outbox checks provide the retained regression evidence.

On 2026-10-08, Enrique separately authorized one live run of those five synthetic draft cases against `gpt-6-luna`. Two drafts passed (open time and taken slot); three fell back safely. The pending request draft used a curly apostrophe, outside the one-segment GSM character set. The cancellation and kept-visit drafts repeated true facts from the safe reply, but the validator rejected their natural word order. The adapter now normalizes common curly punctuation to GSM-safe characters and no longer includes the safe fallback text in the model input. Enrique then chose a minimal deterministic validator that checks one GSM-7 segment and exact dates, times, and references only; status wording follows the model instructions. No SMS or scheduling action was sent or performed by the evaluation.

On 2026-10-08, Enrique authorized one more live run of the five draft cases against `gpt-6-luna` with the minimal validator, from the #275 branch. **All five passed**, with no fallback: the open time, pending request, taken slot, cancelled visit, and kept visit drafts each kept their exact date, time, and reference. This is one run of a nondeterministic model, so it shows the curly-apostrophe and word-order rejections no longer occurred here, not that they cannot recur; the validator still does not check status wording.

On 2026-09-28, a project-scoped key was read from the owner's password manager into an ignored local `.env`; the key was never printed or committed. The seven synthetic cases were run against the OpenAI Responses API:

| Run | Result | Finding |
| --- | --- | --- |
| Original instructions | 2/7 passed | Five ambiguous texts asked questions while retaining actionable intent or target fields. |
| Explicit null-field clarification instructions, two runs | 7/7 each | One broad-window question still offered guessed calendar dates on the second run. |
| Final no-candidate-date instructions, two runs | 7/7 each under the defined harness checks | Ambiguous cases returned `intent=clarify` and null action fields. The broad-window question contained no numeric candidate date, and the observed questions suggested no calendar dates; the exact owner reference proposed approval of A-101. |

On 2026-09-29, the nine #60 cases were run four times against the production prompt: three runs passed 9/9, and one run passed 8/9. The failed case was "next Friday afternoon"; that run's output did not record the resolved fields, which the summary now includes. In the passing runs, it resolved to Friday Oct 9, 12:00–17:00. An unexpected resolution is visible in the offer's full dates and writes nothing. In one run, "Approve X or Y" came back as a clarification that still carried an approval; the adapter rejects that shape, and the service replies with a safe retry message.

Retain `gpt-6-luna` as the **initial pilot interpretation model ID** for #22. This small synthetic evaluation is a gate, not proof of reliable behavior on all real conversations. #22 must validate every model proposal against actor permissions, exact request references, dates, and the authoritative calendar before a write. An uncertain or malformed proposal must ask for clarification or fall back safely. No live SMS or customer data was used in this evaluation, and selecting the model does not authorize either.

### Historical direct-action tool evaluation (#272, #273, #274)

Before #298, this script also covered typed owner intents (`show_requests`, `prepare_counteroffer`, and version-quoted approve and decline) through `classify_owner_reply`. Those owner cases were removed with the classifier and are historical evidence, not a current evaluation path. The client cases remain: `request_booking` for a time just offered, and `confirm_cancel` and `keep_visit` after a cancellation question. Client cases pass the open prompt kind and a synthetic transcript, with Monday 2026-10-12 as "today" for the booking cases. The old owner cases used one or two invented pending requests and checked model proposals, not backend effects:
- An invitation followed by "Oct 13 at 1 pm" must be `availability` for 13:00, never a booking. After an unavailable time, "Oct 13" must be `availability` for that day without a clarification.
- "lovely, lets lock that in" after an offer of Oct 13 at 1:00 PM must be `request_booking` for exactly that time. "can we do 3 pm instead?", "is 1 pm the earliest you have?", and "sounds good" with no offer open must not be.
- "yes please, go ahead" after a cancellation question must be `confirm_cancel`, "actually let's hold onto it" must be `keep_visit`, and a question about Friday must stay `availability`. The same yes with no question open must not be `confirm_cancel`.
- The owner's "what's waiting for me?", "offer Avery Friday at 2pm instead", "approve Blake's", and "no, decline that one" must produce the matching intent with the right reference, version, and offer time. "approve it" with two pending requests and "yes please" after a calendar answer must not approve or decline.

On 2026-10-08 the owner authorized live runs of these cases against `gpt-6-luna` with the production instructions: 31 client cases and 6 owner cases (37 after the date-only invitation case below).
- Three runs on the first version of the cases passed 36/36 each. A review then tightened the two "nothing is open" cases so a lapsed offer or cancel question still sits in the transcript. One of two runs failed: with no open prompt, "yes please, go ahead" after a lapsed cancel question came back `confirm_cancel`. The backend would have written nothing, because no stored prompt exists, but the model should not propose it.
- The instructions now say the Open prompt kind is the only evidence that a time offer or cancellation question is still open. This does not cover a booking invitation, which sets no prompt kind: a date-only reply such as "Oct 13" sent straight to an invitation must still be availability, the friction the tester reported. A review caught that the first wording could undo this, so the date-only invitation case was added.
- Final instruction wording: four runs passed 36/36 on the cases before the invitation case was added. With it (37 cases), two of four runs passed 37/37. The other two each missed one case: the lapsed-cancel-question case again, and the older "Approve a101a101 or b202b202" case (the model named the first reference). Neither miss can write: the backend has no stored prompt for the first, and a model-named decision with two pending requests only asks for the exact command.
- Treat the eval as a sampling check, not a pass/fail certificate. The model is not deterministic; the lapsed-cancel miss appeared in 1 of 8 runs after the instruction change versus 1 of 2 before. A pass shows only that these synthetic texts were read as intended; the backend's stored-state checks still decide every write, and real conversations may differ. These runs evaluate the model's reading of the text, not its reply wording; client reply drafts are covered by the `--drafts` cases above (#283).

### Historical owner reply drafts (#285)

Before #298, `backend/evals/scheduling_messages.py --owner-drafts` defined seven synthetic cases for read-only owner replies. The flag and `valid_owner_draft` have been removed. The separate production read-only calendar drafter is also removed by #299; calendar answers now come from the owner tool loop's final message. **One historical live run** was separately authorized by Enrique on 2026-10-08 against `gpt-6-luna` (key read from the environment only; no SMS or scheduling action): **all seven passed** with no fallback. This one nondeterministic run validates neither the #298 decision tool loop nor the #299 calendar tool.

Current offline checks for #298 cover the model continuation after decision tool results, stale and rejected writes, reply length and GSM limits, replay, and crash recovery. No live tool-loop call was made for #298.

### Owner calendar tool (#299)

The owner loop now exposes `get_calendar(from, to, statuses, offset)`. The tool reads current business-scoped calendar and first-name client records on every call, including a follow-up or MORE; its JSON contains confirmed visits, unexpired pending requests, and unavailable blocks for the requested local range and page. The model uses the owner's sent-aware 24-hour transcript, local date, and timezone to interpret day/week, status, count, follow-up, and MORE wording, then writes the final SMS. The deterministic owner calendar matchers, stored follow-up and paging state, and second read-only drafter are gone. The model's prose can misstate a count or date; that residual risk is accepted and no #285 per-entry owner draft check is applied.

Scripted offline cases cover local date and status selection, current-data rereads for follow-ups and MORE, first-name scoping, ambiguous count clarification, preserving an open counteroffer draft during a calendar read, reply length/GSM failure, and duplicate receipt replay. They exercise the tool boundary with fictional data and do not measure live model reliability. **No live model evaluation or SMS was run for #299.** A future live run requires separate authorization.

### Owner tool-loop synthetic evaluation (#297, #300)

`backend/evals/owner_tool_loop.py` defines three fictional live-model cases for the production owner tool loop: a bare "Approve" with one pending request, a qualified yes after an assistant summary, and a stale-version approval. Its in-memory tools return structured JSON and record calls; they never invoke the scheduling service, outbox, SMS, or AWS. The check requires one approval call with the listed reference and version, no other write tool, and stale-result wording that does not claim success. The final reply is printed for manual review because these checks cannot prove every prose claim. No live run has been made for #297; running it uses the OpenAI API and requires Enrique's separate authorization. After authorization, with `OPENAI_API_KEY` already in the process environment, run:

```sh
cd backend && .venv/bin/python -m evals.owner_tool_loop --live
```

Omit `--live` to list the cases without an API call; `--case approve-one` runs only that fictional case. #300's offline scripted checks cover draft, sent-preview proof, plain-yes send, cancel, revision, stale and expired drafts, consent and slot failures, replay, and client acceptance. The model may still misread an owner's reply or write client text that does not match the owner's intent; backend validation does not check wording semantics. The existing fixed owner notification for a later client-send failure remains by Enrique's decision. No live SMS or deployment was run for #300.

### Client read-tool loop (#340)

The production client loop exposes `get_business_info`, `get_availability`, `get_my_bookings`, (#341) `request_booking`, (#342) `prepare_cancellation`, `confirm_cancellation`, and `dismiss_cancellation`, and (#343) `request_reschedule`, `get_my_counteroffer`, and `respond_to_counteroffer` (see [CONVERSATION.md](CONVERSATION.md#client-read-tool-loop-340)). Offline checks in `backend/tests/test_client_tool_loop.py` use a scripted model at the conversation service, the in-memory calendar and offer memory, and the receipt processor. They cover an ordinary business and hours question, an availability offer that a later reply books through the unchanged deterministic path, truthful no-opening reasons (holiday, weekend, past, reserved by owner) with no other client's detail, own-bookings isolation, rejected cross-client and unknown-tool arguments, a mixed three-tool question and the fourth-call limit, an ambiguous text that only asks, rejected final texts (claimed write, unreturned time, incomplete offer) with fallback and no stale offer, tool errors, a model timeout, closing an old offer, replay of one receipt, and the adapter's call, tool, and rewrite bounds. They show what the backend allows and checks, not how reliably the real model chooses tools or words answers. For #341, `backend/tests/test_client_booking_tool.py` adds offline scripted-model checks: accepting an offered time creates one pending request and a pending reply; a first request offers and cannot book in the same message; no open offer, expired offer, changed visit length, changed hours, and a taken time each book nothing (the taken time is only "reserved by owner" with no other client's detail); an ambiguous or unmatched acceptance keeps the offer open; another client's or a reschedule offer is never accepted; a reply that says confirmed is replaced by the pending notice while the request stays; a loop failure after the write still sends that notice, and after a refusal does not retry; a mixed service question plus acceptance; repeated receipt and replay create one request; malformed arguments. For #342, `backend/tests/test_client_cancellation_tool.py` adds offline scripted-model checks: a prepared question binds one visit and version and cancels nothing, a later yes cancels once and a repeated yes or receipt cancels nothing more; a declined question closes and the visit stays; an unknown, another client's, or ambiguous visit stores nothing and discloses only the sender's own visits; a visit changed, a move request waiting, an expired question, a newer prompt, and no answer each cancel nothing; a pending request is cancelled by the same confirmation; one action per message; a loop failure after a write still sends the cancellation notice once, after a refusal does not retry, and after the question falls back to the deterministic question; and the reply check rejects a premature or unbacked cancellation claim and requires "nothing was cancelled" when none was made. For #343, `backend/tests/test_client_reschedule_tool.py` and `backend/tests/test_client_counteroffer_tool.py` add offline scripted-model checks: a move offer binds one confirmed visit and version, a request leaves the original confirmed and creates one pending replacement, and the owner's approval swaps them; an unknown, another client's, pending, or ambiguous visit stores no offer; an edited visit, a replaced or expired offer, a wrong visit ref, a taken time, a waiting move request, and the two offer kinds mixed each write nothing and are not retried; a repeated receipt requests once; a mixed question and a second action take only one action; a counteroffer accepted in other words creates a pending request and no confirmed visit, a decline books nothing, an unclear answer asks again, a decided source request, a taken time, a newer open question, an expired offer, and another client's offer write nothing; and the reply check rejects premature moved, rescheduled, or confirmed wording and requires the visit to stay confirmed and, for a decline, that nothing was requested. **No live model evaluation, SMS, or deployment was run for #340, #341, #342, or #343.** The live run still needs to check that the model calls `request_booking` only for an accepted earlier offer and asks when the acceptance is unclear, and that it calls `prepare_cancellation` only for one clearly identified own visit, `confirm_cancellation` only for a clear yes to an open question, `dismiss_cancellation` for a no, and neither for an unclear reply; that it calls `get_availability` with the visit `ref` only for a clearly identified confirmed visit to move, `request_reschedule` only for an accepted time of that move offer, and `respond_to_counteroffer` only for a clear yes or no to the owner's open offer (a counter-proposal or a question is neither). The live client cases in `evals/scheduling_messages.py` still test only the intent proposal; the loop's tool choice is not yet covered by a live run. A live run uses the OpenAI API and needs Enrique's separate authorization.

### Client tool-loop live check (#357)

`backend/evals/client_tool_loop.py` runs 19 fictional cases through the production client loop (`OpenAIMessageInterpreter.run_client_loop` with its client instructions and tool schemas, the production `ClientReadTools` dispatcher, and `check_reply`). Fixed synthetic today is Monday 2026-10-12, 10:00 Pacific; the business, service, client, visits, earlier offers, cancellation question, and owner counteroffer are in-memory and invented. The dispatcher, argument validation, availability tool, and reply check are the production code; the write hooks (booking, cancellation, reschedule, counteroffer) are hand-written stand-ins that return the production result shapes for the common outcomes and refusals, but are not the conversation service's code and do not exercise its stored-state checks. It never reaches the scheduling service, outbox, SMS provider, or AWS, and pytest and CI never call it.

Cases: `request_booking` for an accepted earlier offer (by number and in words) and not for an unclear acceptance, a counter-proposal, or a question; `prepare_cancellation` for one clearly identified own visit and not for an ambiguous one; `confirm_cancellation` for a clear yes, `dismiss_cancellation` for a no, and neither for an unclear or unrelated reply; `get_availability` with the visit `ref` for a move and `request_reschedule` only for an accepted time of an open move offer; `respond_to_counteroffer` accept for a clear yes, decline for a clear no, and not for a counter-proposal or a question; and two mixed-intent texts (service question plus booking; cancel plus question). A case passes its tool-choice check when the expected calls (with the listed arguments) appear in order, no other write tool is called, and no write tool is called twice; read tools are always allowed. Each case is one pass of the loop (at most 5 model calls, 3 tool calls), so a full run is at most about 95 small requests. The JSON summary lists, per case, the expected and actual tools with arguments, tool-choice pass or fail, the final reply, whether it passes `check_reply`, and any `check_reply` rejections or loop error. The exit code is nonzero when any tool choice fails, any case ends in an error (a failed model call or an undeliverable reply is reported in `errors` and never counts as a pass), or any final reply fails `check_reply`. Read each reply for wording accuracy, because the check cannot prove every prose claim. The output holds no secret.

With `OPENAI_API_KEY` already in the process environment (never read from or printed to a file or chat), run from the repository root:

```sh
cd backend && .venv/bin/python -m evals.client_tool_loop --live
```

Omit `--live` to list the cases without an API call; `--case cancel-yes` runs one case. Offline checks in `backend/tests/test_client_tool_loop_eval.py` run every case against a scripted model and confirm the runner refuses to call a model without `--live` and a key. **No live run has been made by this change.** Enrique authorized one live run on 2026-10-12; its result is recorded on #333 after that run. A run is one sample of a nondeterministic model, not a guarantee.

### Cutover validation (#275)

The model-led client path is the shipped path: #272 to #274 supply the typed tools, #282 their live evaluation, and #283 the validated reply drafts. #275 removed the superseded read-only drafter and added offline checks for opt-out (the model is not called) and an over-length draft. No new live model run was made for it. The live evidence is the 2026-10-08 runs above, so the open gaps are:
- The minimal draft validator passed one live rerun (five of five); a single run is a sampling check, and status wording is not validated.
- The 37-case tool run is a sampling check (two of four runs passed fully, with misses that cannot write).
- The historical owner draft run (7/7) predates #298–#300 and does not validate the current owner tool loop.
- No deployment, live SMS, or real conversation was used; the AWS handoff stays disabled until Enrique separately authorizes it.

## Instructions changed by #348

The SMS instructions no longer say "home-cleaning business" or use "cleaning" in their intent rules; they refer to "the service" and read the saved `Business:` and `Service:` context lines. The 20-case results recorded under #241 and the 37-case historical results recorded under #272 to #274 were run on the earlier wording. The eval harness now passes a synthetic business and service (`evals/scheduling_messages.py`).

On 2026-10-12, after #350, Enrique ran `python -m evals.scheduling_messages` once against `gpt-6-luna` with the new instructions. The script now lists 28 client cases: the 17 client cases from the #241 suite (its 3 owner cases were removed by #298) plus the 11 client cases added for #272 to #274 (their owner cases were also removed by #298). **All 28 passed.** Cases run: invalid-date, tomorrow, broad-window, missing-month, cancel-two-visits, cant-make-thursday, screenshot-1-bookings-this-week, screenshot-2-confirmed-already, screenshot-3-summary, count-confirmed-next-week, spanish-question, spanish-follow-up, follow-up-only-confirmed, all-upcoming-after-a-week, open-times, request-during-calendar-talk, ambiguous-booking, invited-exact-time-is-offered-first, date-only-reply-to-invitation, date-only-after-unavailable-time, free-form-acceptance-books-offered-time, counter-proposal-is-not-an-acceptance, question-about-offer-is-not-an-acceptance, acceptance-with-no-open-offer-books-nothing, free-form-yes-confirms-cancel, free-form-keep-after-cancel-question, unrelated-question-does-not-confirm-cancel, confirm-cancel-with-no-question-open. The run summary is posted on [#348](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/348#issuecomment-6134596355). This is one run of a nondeterministic model, so it is a sampling check, not a guarantee. It evaluates the model's reading of the text only; the backend's stored-state checks still decide every write. No SMS was sent and no scheduling action was performed.
