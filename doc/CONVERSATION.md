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

1. As `client`, type `Book YYYY-MM-DD` to see available start times. The reply includes the complete `BOOK YYYY-MM-DD HH:MM` command to use next; each text is interpreted on its own.
2. Type `Book YYYY-MM-DD HH:MM` using a returned time. The reply says the request is pending owner approval and gives an eight-character reference.
3. Type `/calendar` to inspect the pending request; `/owner` to switch actors.
4. Type `Approve REFERENCE` or `Decline REFERENCE`. The exact reference must be the one shown. An ambiguous `Yes` cannot change the request.
5. For a confirmed visit, switch to `/client` and type `Cancel REFERENCE`. For a replacement, type `Reschedule REFERENCE to YYYY-MM-DD HH:MM`; the original remains confirmed until the owner approves the replacement.
6. Type `/quit` to exit. The entire calendar resets on restart.

SMS collects only the visit date and time. The owner enters and verifies client profile fields (name, service address, and home size/duration) in the authenticated calendar. A text from a sender without an active, verified profile and matching consent is not authorized for commands. It gets no scheduling reply by SMS, the model is not called, and no scheduling change occurs. If the profile or consent changes after the text arrives, the conversation service refuses the text, and the sender recheck blocks delivery of its reply. A booking request without a complete date and time gets a `BOOK YYYY-MM-DD` prompt and no write. A date-only `BOOK` returns slot suggestions.

The model receives only the inbound text, actor, current local date, timezone, and up to eight short active references. It receives no profile name, phone, address, notes, or API key in its prompt. The model proposal is schema checked and never executes a write directly. Ambiguous model responses and timeouts return a safe question or retry message. Messages over 1,000 characters are rejected intact. `BOOK` and `RESCHEDULE` accept one complete local `YYYY-MM-DD` date or `YYYY-MM-DD HH:MM` time, with no timezone suffix or alternative; the backend rejects daylight-saving gaps and ambiguous times. `APPROVE`, `DECLINE`, and `CANCEL` require one current eight-character reference or exact full ID. Slot suggestions are advisory; the hold command checks availability again transactionally.

The signed Twilio ingress can hand off a verified receipt ID to SQS after persisting it. The worker rereads the stored receipt, rechecks current consent and actor state, and uses the same conversation service as this local exercise. Each SMS scheduling transaction checks STOP markers atomically with the calendar write. A cancelled transaction is terminal for that receipt, so a STOP that is later cleared cannot revive an older request. Noncommitted clarifications and slot suggestions are recorded with a stable receipt-based outbox intent; the sender derives the destination from the verified receipt and rechecks consent, opt-out, and the explicit recipient allowlist. Reply bodies are purged with inbound bodies 90 days after the last exchange. Duplicate webhook or queue delivery cannot create a second logical request or reply.

The AWS handoff is disabled by default. The `EnableSmsConversations` parameter only enables it when signed SMS ingress and SMS sending are also separately authorized; the worker obtains the OpenAI key from a `SecureString` at `/scheduling/<environment>/openai/api-key`. Deploying this code, provisioning that secret, and sending live texts require separate owner authorization. The local exercise above remains entirely synthetic.
