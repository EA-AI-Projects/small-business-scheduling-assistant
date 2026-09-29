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
uses two invented visit/request references (`A-101`, `B-202`) and prints the
model's proposed interpretation, including any clarification question. It
does not send an SMS, change the calendar, or apply the backend permission and
availability checks planned for #22. Do not enter real names, phone numbers,
addresses, access codes, or customer messages. The preview rejects recognizable
phone numbers and access-code phrases before an API call, but this filter cannot
detect every kind of private information.

For local tests, a password manager can supply the value when creating `.env`; no secret value or vault item reference belongs in this repository. Add future local-only variables to the ignored file as their test harnesses need them. The deployed Twilio integration uses an environment-scoped SSM SecureString and has separate SMS authorization gates; a local `.env` does not configure or authorize live messaging.

The script sends seven synthetic texts with no real people, phones, addresses, or access codes. Six are malformed or ambiguous and should request clarification; one is an exact owner reference to check that the model can propose an interpretation without making a decision. The model sees one `propose_message` function. The script provides no scheduling write tool and does not call the calendar service. Backend actor, request-reference, date, and permission validation remains mandatory in #22 even if this evaluation passes.

The response is a JSON summary of the model's proposals. A failed clarification case returns a nonzero exit code and prevents selecting the candidate as the pilot model. Store the summary and model ID in #24 after the live synthetic run; do not store the API key.

## Current result

On 2026-09-28, a project-scoped key was read from the owner's password manager into an ignored local `.env`; the key was never printed or committed. The seven synthetic cases were run against the OpenAI Responses API:

| Run | Result | Finding |
| --- | --- | --- |
| Original instructions | 2/7 passed | Five ambiguous texts asked questions while retaining actionable intent or target fields. |
| Explicit null-field clarification instructions, two runs | 7/7 each | One broad-window question still offered guessed calendar dates on the second run. |
| Final no-candidate-date instructions, two runs | 7/7 each under the defined harness checks | Ambiguous cases returned `intent=clarify` and null action fields. The broad-window question contained no numeric candidate date, and the observed questions suggested no calendar dates; the exact owner reference proposed approval of A-101. |

Retain `gpt-6-luna` as the **initial pilot interpretation model ID** for #22. This small synthetic evaluation is a gate, not proof of reliable behavior on all real conversations. #22 must validate every model proposal against actor permissions, exact request references, dates, and the authoritative calendar before a write. An uncertain or malformed proposal must ask for clarification or fall back safely. No live SMS or customer data was used in this evaluation, and selecting the model does not authorize either.
