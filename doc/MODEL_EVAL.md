# Synthetic scheduling-message model evaluation

Issue [#24](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/24) starts with the owner-selected OpenAI `gpt-6-luna` candidate. OpenAI's [model page](https://developers.openai.com/api/docs/models/gpt-6-luna) confirms the ID and Responses function calling; the [function-calling guide](https://developers.openai.com/api/docs/guides/function-calling) describes the tool schema used here.

Run from `backend/` with a project-scoped `OPENAI_API_KEY` supplied through the process environment:

```sh
python -m evals.scheduling_messages
```

The script sends seven synthetic texts with no real people, phones, addresses, or access codes. Six are malformed or ambiguous and should request clarification; one is an exact owner reference to check that the model can propose an interpretation without making a decision. The model sees one `propose_message` function. The script provides no scheduling write tool and does not call the calendar service. Backend actor, request-reference, date, and permission validation remains mandatory in #22 even if this evaluation passes.

The response is a JSON summary of the model's proposals. A failed clarification case returns a nonzero exit code and prevents selecting the candidate as the production model. Store the summary and model ID in #24 after the live synthetic run; do not store the API key. No production model has been selected yet.

## Current result

Not run against the OpenAI API: this task environment has no `OPENAI_API_KEY`. Local harness tests validate the request shape and reject missing or multiple tool calls; they are not evidence of model behavior. Enrique has been asked to configure a project-scoped key or identify the existing secret path. Until the live synthetic run is recorded, #24 remains open and #22 remains blocked on it.
