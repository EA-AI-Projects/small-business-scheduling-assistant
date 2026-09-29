# Local conversation exercise and command boundary

Issue [#22](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/22) is in progress. The current local exercise uses a fictional verified client, fictional owner, and an in-memory calendar. It calls OpenAI to interpret other scheduling messages, but state-changing `BOOK`, `RESCHEDULE`, `APPROVE`, `DECLINE`, and `CANCEL` commands require exact affirmative syntax parsed locally. Every proposed action is checked against actor, reference, current appointment state, and the scheduling domain before a write. Writes produce the existing transactional outbox intents in memory. This exercise does not use Twilio, DynamoDB, real customer records, or live SMS.

With the backend dependencies installed as in the [README](../README.md#local-backend) and a local ignored `.env` containing `OPENAI_API_KEY`, run from the repository root:

```sh
(
  set -a
  source .env
  set +a
  cd backend
  .venv/bin/python -m scheduling.local_conversation
)
```

The subshell removes the key from the parent shell even if the command is interrupted. Use only invented messages. Recognizable phone numbers and entry/access-code phrases are rejected locally; this filter cannot detect every kind of private data.

Try a weekday within the next 14 days:

1. As `client`, type `Book YYYY-MM-DD` to see available start times.
2. Type `Book YYYY-MM-DD HH:MM` using a returned time. The reply says the request is pending owner approval and gives an eight-character reference.
3. Type `/calendar` to inspect the pending request; `/owner` to switch actors.
4. Type `Approve REFERENCE` or `Decline REFERENCE`. The exact reference must be the one shown. An ambiguous `Yes` cannot change the request.
5. For a confirmed visit, switch to `/client` and type `Cancel REFERENCE`. For a replacement, type `Reschedule REFERENCE to YYYY-MM-DD HH:MM`; the original remains confirmed until the owner approves the replacement.
6. Type `/quit` to exit. The entire calendar resets on restart.

The model receives only the inbound text, actor, current local date, timezone, and up to eight short active references. It receives no profile name, phone, address, notes, or API key in its prompt. The model proposal is schema checked and never executes a write directly. Ambiguous model responses and timeouts return a safe question or retry message. Messages over 1,000 characters are rejected intact. `BOOK` and `RESCHEDULE` accept one complete local `YYYY-MM-DD` date or `YYYY-MM-DD HH:MM` time, with no timezone suffix or alternative; the backend rejects daylight-saving gaps and ambiguous times. `APPROVE`, `DECLINE`, and `CANCEL` require one current eight-character reference or exact full ID. Slot suggestions are advisory; the hold command checks availability again transactionally.

This is a local code slice, not the finished SMS conversation path. The signed Twilio ingress still records verified receipts without invoking this conversation service. A durable receipt-processing path and safe outbound clarification/suggestion delivery are required before #22 can close or live SMS can be considered. Existing SMS-send authorization gates remain disabled unless separately authorized.
