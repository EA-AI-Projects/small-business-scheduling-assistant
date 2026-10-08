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
`/client` or `/owner` to switch perspectives and `/quit` to exit. The preview
uses two invented visit/request references (`a101a101`, `b202b202`) and prints the
model's proposed interpretation, including any resolved dates and clarification
question. It does not send an SMS, change the calendar, or apply the backend
permission and availability checks. Do not enter real names, phone numbers,
addresses, access codes, or customer messages. The preview rejects recognizable
phone numbers and access-code phrases before an API call, but this filter cannot
detect every kind of private information.

For local tests, a password manager can supply the value when creating `.env`; no secret value or vault item reference belongs in this repository. Add future local-only variables to the ignored file as their test harnesses need them. The deployed Twilio integration uses an environment-scoped SSM SecureString and has separate SMS authorization gates; a local `.env` does not configure or authorize live messaging.

The script sends synthetic texts (nine from #24 and #60, plus the #241 calendar cases below) with no real people, phones, addresses, or access codes, using the production instructions, tool schema, and model input from `scheduling.adapters.openai_messages`. "Today" is fixed at Monday 2026-09-28. Since #60, the model resolves relative dates instead of refusing them, so the checks are:

- An impossible date (February 30) must clarify with every action field null.
- "Tomorrow" must resolve to 2026-09-29. "Next Friday afternoon" may clarify, or resolve to one Friday (Oct 2 or Oct 9) within 12:00–17:00. "Tuesday around 3" may clarify, or resolve to 2026-09-29 within 14:00–16:00.
- The owner's "Yes" with two pending requests, and "Approve X or Y", must not name a reference.
- "Cancel my appointment" with two visits must name neither a reference nor a day. "I can't make Thursday" must propose `cancel` for 2026-10-01 without a reference.
- An exact owner reference must propose that reference and decision.

The model sees one `propose_message` function. The script provides no scheduling write tool and does not call the calendar service. A passing run does not replace backend validation: offers write nothing, and only a deterministic reply to a stored offer or confirmation changes the calendar.

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

### Direct-action tools (#272, #273, #274)

The script now also covers the typed intents the backend honors only against stored state: `request_booking` for a time the assistant just offered, `confirm_cancel` and `keep_visit` after a cancellation question, and the owner tools (`show_requests`, `prepare_counteroffer`, version-quoted approve and decline). Client cases pass the open prompt kind and a synthetic transcript, with Monday 2026-10-12 as "today" for the booking cases. Owner cases call the production `classify_owner_reply` with one or two invented pending requests and no shown calendar range or view. They check what the model proposes, not what the backend does:
- An invitation followed by "Oct 13 at 1 pm" must be `availability` for 13:00, never a booking. After an unavailable time, "Oct 13" must be `availability` for that day without a clarification.
- "lovely, lets lock that in" after an offer of Oct 13 at 1:00 PM must be `request_booking` for exactly that time. "can we do 3 pm instead?", "is 1 pm the earliest you have?", and "sounds good" with no offer open must not be.
- "yes please, go ahead" after a cancellation question must be `confirm_cancel`, "actually let's hold onto it" must be `keep_visit`, and a question about Friday must stay `availability`. The same yes with no question open must not be `confirm_cancel`.
- The owner's "what's waiting for me?", "offer Avery Friday at 2pm instead", "approve Blake's", and "no, decline that one" must produce the matching intent with the right reference, version, and offer time. "approve it" with two pending requests and "yes please" after a calendar answer must not approve or decline.

On 2026-10-08 the owner authorized live runs of these cases against `gpt-6-luna` with the production instructions: 31 client cases and 6 owner cases (37 after the date-only invitation case below).
- Three runs on the first version of the cases passed 36/36 each. A review then tightened the two "nothing is open" cases so a lapsed offer or cancel question still sits in the transcript. One of two runs failed: with no open prompt, "yes please, go ahead" after a lapsed cancel question came back `confirm_cancel`. The backend would have written nothing, because no stored prompt exists, but the model should not propose it.
- The instructions now say the Open prompt kind is the only evidence that a time offer or cancellation question is still open. This does not cover a booking invitation, which sets no prompt kind: a date-only reply such as "Oct 13" sent straight to an invitation must still be availability, the friction the tester reported. A review caught that the first wording could undo this, so the date-only invitation case was added.
- Final instruction wording: four runs passed 36/36 on the cases before the invitation case was added. With it (37 cases), two of four runs passed 37/37. The other two each missed one case: the lapsed-cancel-question case again, and the older "Approve a101a101 or b202b202" case (the model named the first reference). Neither miss can write: the backend has no stored prompt for the first, and a model-named decision with two pending requests only asks for the exact command.
- Treat the eval as a sampling check, not a pass/fail certificate. The model is not deterministic; the lapsed-cancel miss appeared in 1 of 8 runs after the instruction change versus 1 of 2 before. A pass shows only that these synthetic texts were read as intended; the backend's stored-state checks still decide every write, and real conversations may differ. These runs evaluate the model's reading of the text, not its reply wording; client reply drafts are covered by the `--drafts` cases above (#283).

### Owner reply drafts (#285)

`backend/evals/scheduling_messages.py --owner-drafts` defines seven synthetic cases for the three read-only owner results (a day's calendar answer, a page that continues, a calendar answer with an open offer, a pending summary, a single request detail with a prior transcript, and two how-to replies). It uses the production owner draft adapter and `valid_owner_draft`. The model writes the whole message from the facts and the replies the backend will honor. **No live run has been made**: it needs Enrique's separate authorization, and `.env` was not read. A pass means only the basic check described in CONVERSATION.md: it does not verify names, statuses, counts, whether the offer reminder or paging line was included, or whether command wording is right. Enrique accepted that residual risk (#285, 2026-10-08).

Offline scripted conversations (`test_owner_reply_drafts.py`, `test_client_reply_contract.py`) cover: valid drafts used as the whole trimmed text; invented or missing dates, times, and references; a wrong year; swapped references, dates, and times within and across sentences; a restated swap in a sentence with no reference; over-length and non-GSM text; timeouts, malformed output, and a throttled (non-OSError) history read, each sending the unchanged backend text; an open offer whose time and request reference are accepted as facts and whose fallback is unchanged; model-written YES/MORE/APPROVE prompts being accepted; out-of-scope results never reaching the drafter; opt-out and non-owner senders never reaching the model; the 24-hour transcript reaching both owner model calls; and a duplicate inbound delivery drafting once.

### Cutover validation (#275)

The model-led client path is the shipped path: #272 to #274 supply the typed tools, #282 their live evaluation, and #283 the validated reply drafts. #275 removed the superseded read-only drafter and added offline checks for opt-out (the model is not called) and an over-length draft. No new live model run was made for it. The live evidence is the 2026-10-08 runs above, so the open gaps are:
- The minimal draft validator passed one live rerun (five of five); a single run is a sampling check, and status wording is not validated.
- The 37-case tool run is a sampling check (two of four runs passed fully, with misses that cannot write).
- Owner replies are fixed backend text except the three read-only kinds drafted since #285 (calendar answer, pending-request summary, how-to); those have offline checks only and no live run.
- No deployment, live SMS, or real conversation was used; the AWS handoff stays disabled until Enrique separately authorizes it.
