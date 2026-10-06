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

Since #241 the model, not keyword rules, decides whether a client text is a calendar question (owner decision, 2026-10-05). The script adds ten client cases. Follow-up cases send the same "Last calendar answer" line the service sends:
- The three texts from the #241 screenshot must be `calendar_question`. The second must ask only about confirmed visits, and the third is a summary.
- "How many confirmed visits next week?" must be a confirmed-only count for 2026-10-05 to 2026-10-11.
- A Spanish question and a Spanish follow-up ("¿Y la próxima semana?") must be calendar questions with the right range.
- "Just the confirmed ones" after an answer must change only the statuses.
- "What times are open Friday?" and "Can I get a cleaning Friday instead?" must stay availability requests.
- "Booking for Friday?" must clarify.

On 2026-10-05 the owner ran all 20 cases against `gpt-6-luna` with the #245 instructions (no key or message content left the owner's machine except the synthetic texts): **18/20 passed**.
- All three screenshot texts, both Spanish cases, the count, the status-only follow-up, and "Show me all my upcoming visits" (`range_scope: all_upcoming`) passed. Follow-ups left unchanged fields null or `keep`, as instructed.
- Missed: "Can I get a cleaning Friday instead?" during a calendar conversation came back `clarify_booking` (the client would get one extra check-or-request question before the offer).
- Missed: "Booking for Friday?" came back `availability` (the client would get Friday's open times instead of the check-or-request question #240 requires).
- Neither miss can write. #246 adds instruction examples for both; record its rerun here.

## Current result

On 2026-09-28, a project-scoped key was read from the owner's password manager into an ignored local `.env`; the key was never printed or committed. The seven synthetic cases were run against the OpenAI Responses API:

| Run | Result | Finding |
| --- | --- | --- |
| Original instructions | 2/7 passed | Five ambiguous texts asked questions while retaining actionable intent or target fields. |
| Explicit null-field clarification instructions, two runs | 7/7 each | One broad-window question still offered guessed calendar dates on the second run. |
| Final no-candidate-date instructions, two runs | 7/7 each under the defined harness checks | Ambiguous cases returned `intent=clarify` and null action fields. The broad-window question contained no numeric candidate date, and the observed questions suggested no calendar dates; the exact owner reference proposed approval of A-101. |

On 2026-09-29, the nine #60 cases were run four times against the production prompt: three runs passed 9/9, and one run passed 8/9. The failed case was "next Friday afternoon"; that run's output did not record the resolved fields, which the summary now includes. In the passing runs, it resolved to Friday Oct 9, 12:00–17:00. An unexpected resolution is visible in the offer's full dates and writes nothing. In one run, "Approve X or Y" came back as a clarification that still carried an approval; the adapter rejects that shape, and the service replies with a safe retry message.

Retain `gpt-6-luna` as the **initial pilot interpretation model ID** for #22. This small synthetic evaluation is a gate, not proof of reliable behavior on all real conversations. #22 must validate every model proposal against actor permissions, exact request references, dates, and the authoritative calendar before a write. An uncertain or malformed proposal must ask for clarification or fall back safely. No live SMS or customer data was used in this evaluation, and selecting the model does not authorize either.
