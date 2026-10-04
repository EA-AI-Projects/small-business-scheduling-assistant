# SMS conversation flow and command boundary

Clients and the owner can text in plain language. The model interprets the text; the calendar changes only when a reply matches something the system itself offered. Issue [#60](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/60) recorded the owner decisions behind this flow. Everything below is exercised with fictional data, an in-memory calendar, and no Twilio, DynamoDB, or live SMS.

## How a conversation works

| Step | Example text | What happens |
| --- | --- | --- |
| Ask | "Hi. Do you have availability for tomorrow?" | The model resolves "tomorrow" to a date. The backend checks the 14-day horizon, holidays, working hours, and existing visits, then offers 3–5 real start times. Nothing is written. |
| Pick | "10 works", "the 10 o'clock one", "option 2", or "yes" when one time was offered | The reply must map to exactly one offered option. A pending request is created, and the reply states its full date and time and that the owner must approve it. |
| Approve | Owner: "yes", "approve", or "decline" | Acts only when exactly one request is pending. With several, the reply lists them and asks for `APPROVE REF` or `DECLINE REF`. |
| Cancel | "I can't make Thursday" | "Cancel your Thu Oct 1 at 9:00 AM visit? Reply YES to confirm." Only a plain yes cancels; "no" keeps the visit. If several visits match, they are listed by number; picking one ("2", "the 11 one") leads to the same YES question. |
| Reschedule | "Can I move Thursday to Friday morning?" | Replacement times are offered the same way. Picking one creates a pending replacement; the original stays confirmed until the owner approves it. "I need to reschedule" first asks which day. |

An offer or confirmation question stays valid for **30 minutes**. A later answer gets "That offer expired after 30 minutes, so nothing changed" and a prompt to ask again. Any new request replaces the previous offer.

When a specific time is open, the offer is that single time, answered with "yes". When it is not, the three nearest open times are offered instead. Offers prefer on-the-hour starts, then half-hour starts, and spread across the requested days.

## What cannot cause a write

- **Model output never selects a write.** The model proposes an intent, a resolved day range, a time window, and the day of an existing visit. Its output is schema-validated: dates must be `YYYY-MM-DD`, times `HH:MM`, and a clarification carries no action fields. The backend rejects impossible dates, past dates, and days beyond the booking horizon before any offer.
- **Replies are matched deterministically.** A number, a time ("11", "11am", "2:30 pm", "noon"), an ordinal ("the second one"), "option N", a day name that narrows the offer ("Thursday at 8am"), or a plain "yes" for a single option. A reply containing a negation ("not 10", "can't"), a question mark, an alternative ("10 or 12"), or a change of plan ("next week", "instead") is not treated as a pick; it goes to the model, which can only offer new times or ask a question.
- **Ambiguous picks ask again.** "2" is ambiguous when option 2 and 2 PM are different offered times; "8am" is ambiguous when 8:00 AM is offered on two days. The reply asks for "option N" or a time with AM or PM, and nothing is booked. A time that was not offered is refused the same way.
- **Confirmations are rechecked.** A cancellation confirmation is refused if the visit changed after the question. A replacement pick is refused if the original visit is no longer confirmed. Every booking re-checks availability transactionally, so a time taken in the meantime is reported, not double-booked.
- **The owner has no offer memory.** Only a client's own offer can be answered, and only by the phone it was sent to.
- **The #24 malformed and ambiguous cases still change nothing**, even when a fake model returns an unsafe proposal for them.

Exact commands keep working and bypass the model: `BOOK YYYY-MM-DD` (offers that day's times), `BOOK YYYY-MM-DD HH:MM`, `RESCHEDULE REF to YYYY-MM-DD[ HH:MM]`, `CANCEL REF`, `APPROVE REF`, and `DECLINE REF`. They accept one complete local date or time with no timezone suffix or alternative; the backend rejects daylight-saving gaps and ambiguous times.

## Conversation memory

The last offer, list of visits, or confirmation question is stored per sender phone. It holds only the offered or listed start instants, or one appointment ID and version, plus its 30-minute expiry. It holds no message text.

- **Local harnesses** keep it in process memory; it resets on restart.
- **The cloud worker** stores it as one `SMS_STATE#<phone>` item per sender in the business table. Reads use strong consistency and enforce expiry themselves. DynamoDB TTL on `expires_at_epoch` removes stale items later; TTL deletion can lag, so it is cleanup, not the expiry check. Clearing a prompt is conditional on its ID, so an older reply cannot erase a newer offer.

## Owner calendar questions

The verified owner can ask read-only calendar questions by text, such as "What is next week looking like?", "What clients do I have tomorrow?", or "How many bookings do we have for Friday?" (PRD 6.6). These are fixed question types in `owner_calendar_questions.py`. Complete questions and follow-ups are matched deterministically, do not use the model, and cannot write. Outside a calendar conversation, approve and decline replies are handled first and work as before; inside one, an approval-like reply is classified as described below.

- **Grounded.** Every answer is read from the calendar and client records at that moment, so a follow-up after a calendar change reflects the change. Nothing is listed that is not in the records.
- **Dates and times.** Answers name the dates and show times in the business timezone. A day runs midnight to midnight in that timezone; a block that crosses midnight appears on both days and counts once in a total.
- **Statuses.** Confirmed visits, pending requests (unexpired holds only), and unavailable blocks are labeled separately. Declined, cancelled, and expired items are not shown.
- **Counts say what they counted.** A count that names a status ("pending requests", "confirmed visits", "including pending") uses it. A bare "bookings", "visits", or "appointments" count does not state a status, and what a booking means is not assumed: the assistant asks "confirmed visits only, pending requests only, or both?" and then counts. Anything else present in the range is reported as not counted.
- **Clarifying questions.** A missing day or week, or a count with no stated status, gets one focused question. While it is open, "confirmed", "pending", or "both" answers it.
- **Approval-like replies are classified by the model, then validated.** While a calendar conversation is open (30 minutes) and a request is pending, a reply that contains an approval-like word ("yes please", "confirmed", "ok thanks") may mean "approve that request" or "keep showing the calendar". It goes to the model with small bounded context: the last assistant message type, the calendar view, range and statuses shown, the request last named (ref, client first name, time), and the pending requests (ref, first name, time). No phone numbers, addresses, full names, or history are sent. The model returns a structured proposal: `approve_named_request`, `decline_named_request`, `calendar_followup` (with statuses or a date range), or `unclear`, with a confidence. It never approves by itself. The backend acts on an approve or decline only when the confidence is high, the proposed ref is the request the assistant last asked about, that request is the single pending one, and its version is unchanged. Otherwise it asks one focused question naming the current request ("Do you mean approve <client, time, ref>, or something about the calendar?") and writes nothing; that question is what names the request, so a first "yes please" never approves. `unclear`, low or medium confidence, a model error, a timeout, or no model adapter also ask and write nothing. A calendar follow-up is applied only with valid statuses and, if a range is given, a complete one of at most 31 days; a half-stated range asks. A request counts as named only when the question that named it was durably saved (checked against the stored reply for the asking message) and only for a message received after the question was asked. The asking message's own redelivery, an earlier message, a lost question, or a deployment that cannot check saved replies all ask again and approve nothing; the check does not depend on message order. A complete calendar question ("How many confirmed visits next week?") is answered without the model. An exact `APPROVE <ref>` or `DECLINE <ref>` works without the model, and with no calendar conversation open, owner approve and decline replies behave as before.
- **Follow-ups.** Within 30 minutes, short messages narrow or expand the last question ("just the pending ones", "what about Friday?", "how many?", "by day"). "next Friday" means the Friday of next Monday-to-Sunday week; the answer always states the date.
- **Length and MORE.** A reply stays within 480 characters (three GSM-7 segments) and whole entries are never cut: the reply says "Showing 1-5 of 20. Reply MORE for the rest." MORE resumes from the stored position only if the entry list is unchanged; if the calendar changed in between it says so and restarts from the first entry, so nothing is skipped or repeated. A redelivered MORE repeats its page.
- **Daylight saving.** On the day clocks change, times carry PDT or PST so a repeated hour reads unambiguously.
- **Memory.** The last question type, date range, and status names are kept per sender for follow-ups, with no message text: in process memory locally, and as one `OWNER_QUESTION#<phone>` item in the business table in the cloud (strongly consistent reads, expiry enforced on read, TTL cleanup).

## Try it locally

For texts together with the owner web app on one shared calendar, use the text simulator described in the [README](../README.md#try-the-app-locally). The terminal exercise below has its own separate calendar.

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

1. As `client`, ask "Do you have anything tomorrow morning?" and reply with one of the offered times.
2. Type `/calendar` to see the pending request, then `/owner` and `yes`.
3. Switch back with `/client`, text "I can't make it tomorrow", and reply `yes`.
4. Type `/quit` to exit. The calendar and conversation memory reset on restart.

Without `OPENAI_API_KEY`, the simulator answers plain language with a question and a note; exact commands and replies to an existing offer still work.

## Profiles, privacy, and the cloud path

SMS collects only the visit date and time. The owner enters and verifies client profile fields (name, service address, and home size/duration) in the authenticated calendar. A text from a sender without an active, verified profile and matching consent is not authorized for commands. It gets no scheduling reply by SMS, the model is not called, and no scheduling change occurs. If the profile or consent changes after the text arrives, the conversation service refuses the text, and the sender recheck blocks delivery of its reply.

The model receives only the inbound text, actor, current local date and weekday, timezone, booking horizon, and up to eight short active references. It receives no profile name, phone, address, notes, or API key. Model timeouts and malformed output return a safe retry message. Messages over 1,000 characters are rejected intact.

The signed Twilio ingress can hand off a verified receipt ID to SQS after persisting it. The worker rereads the stored receipt, rechecks current consent and actor state, and uses the same conversation service as the local exercise, with the DynamoDB conversation memory above. Each SMS scheduling transaction checks STOP markers atomically with the calendar write. A cancelled transaction is terminal for that receipt, so a STOP that is later cleared cannot revive an older request. Offers, questions, and other noncommitted replies are recorded with a stable receipt-based outbox intent; the sender derives the destination from the verified receipt and rechecks consent, opt-out, and the fictional-number refusal. Committed changes are announced by the existing notification texts, which state the full date and time. Reply bodies are purged with inbound bodies 90 days after the last exchange. Duplicate webhook or queue delivery cannot create a second logical request or reply. Ingress never passes STOP, HELP, START, or UNSTOP texts to scheduling (the app's STOP words exclude CANCEL, so a bare "Cancel" is an ordinary message, matching dev's Twilio Advanced Opt-Out keywords, #91), but it honours a Twilio `OptOutType` of `START` only for the body START or UNSTOP, so a "Reply YES" answer is still read as an ordinary reply; a STOP or HELP provider tag always wins.

The AWS handoff is disabled by default. The `EnableSmsConversations` parameter only enables it when signed SMS ingress and SMS sending are also separately authorized; the worker obtains the OpenAI key from a `SecureString` at `/scheduling/<environment>/openai/api-key`. Enabling TTL on the table is part of the stack template. Deploying this code, provisioning that secret, and sending live texts require separate owner authorization.
