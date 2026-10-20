# SMS conversation flow and command boundary

**Status:** Current repository behavior; reviewed 2026-10-14. Model interpretation is separate from backend authorization and scheduling writes. Live messaging requires the operational gates in [DEV_SMS_PLAN.md](DEV_SMS_PLAN.md).

## Recent message context (#270)

The application builds sender-scoped, sent-aware history from durable inbound receipts and provider-accepted outbound records. Drafts and failed sends do not appear. History has a rolling 24-hour window, at most 24 whole messages and 12,000 characters, preferring the newest exchange and latest eligible invitation. Reads query at most 64 recent pointers before fetching originals. Oversized entries are omitted rather than truncated.

Client history requires the same business, verified phone and client ID; owner history uses the business/owner phone. Model context includes current business-local date/time, timezone, booking horizon, open prompt kind and bounded active references. Profile names, phones, addresses, notes and credentials are excluded from client model context. Saved business/service/support facts are available through scoped context/tools. Messages over 1,000 characters are refused intact. Transcript text remains conversation data, separate from system instructions.

## How a conversation works

| Action | Current behavior |
| --- | --- |
| Ask for a time | Read current availability and store an offer. An exact open time is offered alone; otherwise return alternatives. No reservation is made yet. |
| Accept an offer | A later message must match one option in this sender's stored, unexpired offer. Recheck duration, calendar and eligibility; create a pending owner-approval request. |
| Approve/decline | Owner exact command or model decision names one current request/version; lifecycle validation commits the result. |
| Cancel | Resolve one own visit/request and store a version-bound confirmation question. A later confirmation cancels it; declining keeps it. |
| Move | Offer times for one confirmed visit/version, then create a pending replacement; the original stays booked until owner approval. |
| Ask about visits | Read only the client's own current visits or the owner's business calendar; no booking action follows merely from an answer. |

Offers and confirmation questions last 30 minutes. A later expired/stale/replaced/unmatched reply writes nothing. A changed default duration invalidates an offer quoted at the previous length. A new request replaces the previous prompt. Offered starts (the tool and the deterministic offer alike) put business-wide preferred valid starts inside the requested window first and fill the rest with other valid starts, without telling the client why (#382); an explicit window is honored without a preferred start, a preference read failure gives the ordinary offer, and a preference change never invalidates a sent offer. Exact commands retain their deterministic path; a bare answer to a read-only calendar list cannot select or cancel a visit.

## Client calendar questions

The model interprets wording/language, dates, statuses, list/count and follow-ups. Replies are in English and use current client-scoped confirmed visits and live pending requests that have not ended, never another client's records, owner blocks or notes. More than eight active visits takes the contact-owner path. "My next appointment" means the earliest upcoming confirmed visit; an earlier pending request is mentioned separately as awaiting approval.

The deterministic calendar path validates ranges (at most 62 days), refuses past-only/reversed ranges, and clips a partially past range to today. Follow-up memory retains only range/status/view terms for 30 minutes, not appointment facts; each answer rereads visits. A calendar question closes earlier client offers, visit selections or cancellation questions, saying so. It leaves an unanswered owner counteroffer open. While that calendar context is open, only a plain YES/NO reaches the counteroffer before model interpretation; status or list follow-ups cannot accept it.

## Current client routing

After verified-profile/consent/opt-out and exact-command checks, plain counteroffer YES/NO/time matching may answer its current offer. Otherwise, a free-text reply to an open new/move offer, cancellation question, move-day question or counteroffer tries the client tool loop before deterministic fallback. Other messages get one typed intent proposal; supported intents may enter the same loop. At most one loop runs per receipt. On failure, the existing intent/state handler and result-based drafter provide the fallback; exact commands and stored-prompt matchers remain supported.

Duplicate receipts replay stored results/replies. A failed draft never retries an already refused or committed scheduling action. The cloud worker uses the same conversation service as local exercises with DynamoDB state.

## Client read-tool loop (#340)

The client loop has five model calls, three tool calls and a 30-second budget, with no provider session state. Strict tools derive client/business from the verified receipt and reject unexpected arguments.

| Tool | Result / write boundary |
| --- | --- |
| `get_business_info` | Saved business, support, service, hours/closures, horizon and approval policy. Missing profile is unconfigured, never invented. |
| `get_availability` | Current start options, at most five, and safe no-opening reasons; stores one 30-minute offer with its quoted length. A visit reference binds a move offer. Occupied time is only "reserved by owner". |
| `get_my_bookings` | This client's own upcoming confirmed and unexpired pending visits, references and local times. |
| `request_booking` | Local date/time must match one previously offered new-booking option; creates a pending hold only. |
| `prepare_cancellation` | Own current visit reference; stores one appointment/version question and cancels nothing. |
| `confirm_cancellation` | No target argument; acts only on that sender's current unexpired confirmation and version. |
| `dismiss_cancellation` | Closes that confirmation, cancels nothing. |
| `request_reschedule` | Visit reference and offered local date/time must match the stored move offer and current original version; creates a pending replacement. |
| `get_my_counteroffer` | Only this client's current unexpired owner offer and source request. |
| `respond_to_counteroffer` | Accept/decline only, bound to the client's current offer; acceptance creates one pending linked request, decline books nothing. |

A first booking message offers times and cannot book in that same message. New-booking and move offers cannot substitute for each other. Holds recheck profile, consent, opt-out, quoted service duration, policy, calendar and receipt-keyed idempotency. Cancellation needs a second message, and a confirmed original with an active replacement is refused until the replacement is withdrawn. A stale original never permits a move.

Only one action takes effect per message. Reads can accompany it, such as a question answered while a cancellation confirmation is prepared. A second availability tool cannot replace the offer already stored by this message. Unknown/malformed tools, excessive calls and repository failures return structured errors.

Counteroffer acceptance uses its own stable idempotency key, so duplicate/repeated answers return the same pending request. An expired/changed source, newer question or taken time writes nothing. An accepted replacement does not confirm either visit; owner approval resolves both according to [SCHEDULING_CONTRACTS.md](SCHEDULING_CONTRACTS.md#client-acceptance-176).

### Final text and fallback

The client loop final reply must be nonempty GSM-7 and at most 480 septets. Every explicit absolute date, time and reference must come from a successful tool result for that message. Relative-date arithmetic, status phrasing, associations among facts and option ordering/numbering are left to the model. Instructions ask for clear offers and approval wording; these are guidance, not additional backend checks. The backend cannot prove prose claims without explicit facts.

A rejected reply gets one rewrite without tools. A second failure/timeout clears any new offer and falls back to the deterministic result without losing or repeating a committed action. Deterministic fallback lists/numbers the stored offer. If model presentation differs, fallback number matching can select a different stored option; that accepted wording risk still results only in a request pending owner approval.

A no-tool answer about an open offer leaves it open. Otherwise, when the loop stores no new offer, an older offer/confirmation is closed with a notice. A new offer supersedes an owner counteroffer only after the final reply passes validation; failed loops leave that counteroffer open.

The legacy result-based client drafter uses a different bound: one GSM-7 segment and exact explicit dates/times/references from the backend result. It does not validate every status claim or fact association. Invalid output uses safe deterministic text. It remains a fallback path, not an independent source of scheduling authority.

## Owner approval and decline tool loop (#298)

After owner verification, opt-out and exact-command checks, free text enters one bounded model loop with the business-local date/time and sent-aware history. Limits are five model calls, three tool calls and 36 seconds. A history/model failure gets the fixed failure reply.

Tools are `get_business_info`, `list_pending_requests`, `get_calendar`, `approve_request`, `decline_request`, `draft_counteroffer`, `send_counteroffer` and `cancel_counteroffer`. Identity derives from the receipt. Reads are scoped to the business; approval/decline require one current request/reference and version and use transactional lifecycle checks. There is no short-reply allowlist or deterministic owner paging/parser state. Unclear targets require clarification. The backend validates a called write; it does not validate every model interpretation or prose assertion.

An owner counteroffer draft binds one pending request/version and a valid alternative time, storing exact model-written client text for 30 minutes without sending. Send requires trusted history showing that an owner preview with that exact client text was actually sent after draft creation. It rechecks request, consent/eligibility, availability, expiry and newer-request guards before atomically confirming and queueing one client intent. The sender repeats checks at handoff and sends the stored text without a new model rewrite. Failure can queue the fixed owner delivery-failure notice. See [SCHEDULING_CONTRACTS.md](SCHEDULING_CONTRACTS.md#owner-counteroffer-confirmation-175).

The model's final owner reply must be nonempty GSM-7 at most 480 septets, with one no-tool rewrite on format failure, then the fixed failure line. Unlike the client loop, there is no per-fact owner date/time/reference validator. A committed action stays committed if reply generation fails. Queueing is distinct from provider delivery.

## Owner calendar questions (#299)

`get_calendar` takes a business-local range, statuses and offset, rereading current confirmed visits, live pending requests and blocks on every call, including MORE. It exposes first names only, not client phones/addresses/notes. Cross-midnight blocks appear on both days but count once in a total. Model context supplies follow-up meaning; pages are current reads, not a fixed snapshot. A calendar read leaves an open counteroffer draft intact. Model count/date/status wording retains the owner loop's accepted semantic risk.

## Profiles, privacy, and the cloud path

SMS scheduling collects requested dates/times, not client profile edits or durable notes. An inactive/unverified/unconsented client is refused before model calls and receives no scheduling SMS reply. Saved business/service names supply factual wording; without a configured default service, SMS booking writes nothing and asks the client to contact the owner. Existing-visit notifications can use neutral wording when a profile is absent. Support escalation reads the saved support contact; references are supplied only from relevant scoped results.

Signed ingress persists receipts before queue handoff. The worker rereads them and checks current actor/consent/opt-out. SMS scheduling transactions check STOP markers atomically; a cancelled transaction is terminal for that receipt even if STOP is later cleared. A noncommitted answer has one receipt-keyed outbox intent. A committed client action already has an atomic notification; a validated draft can attach to that same still-pending intent, never a second one. A crash or sender race leaves the original safe notification. Sender checks event version and current recipient state before using an attached draft.

After each receipt finishes processing, its newly saved replies and scheduling notifications are immediately handed to the sending queue, after any draft attachment. A handoff failure does not rerun the model or undo the action; the minute-based outbox sweep recovers saved work and manages later retries. No extra acknowledgment text is sent. This removes the normal wait for the sweep, but model processing and provider/carrier delivery still contribute to response time.

Bodies follow the client-thread/owner-message 90-day retention rules in [ARCHITECTURE.md](ARCHITECTURE.md#7-privacy-retention-and-erasure). Receipt IDs retain deduplication; erasure fences and hashed provider-message markers block delayed work. Provider STOP/HELP replies remain separate from conversational scheduling. [DEV_SMS_PLAN.md](DEV_SMS_PLAN.md) owns keyword configuration and live gates.

Routine replies aim for one segment, with longer lists when needed for clarity. Unnecessary short references and repeated help tails are omitted; exact commands and requested/disambiguating references remain available. Client cancellation receipts name the cancelled time; owner notices identify client/service/time. Owner cancellations tell the client that the business cancelled the visit.

## Scheduled booking invitations (#249)

The repository implements owner-configured outreach behind separate disabled-by-default selection/delivery gates. Live enablement must be verified separately. The owner chooses enabled/disabled, one local weekday and time, and a lookahead of 1 or 2 weeks. A run uses the business timezone and a fixed run instant `R`; its appointment window ends at the same local wall-clock time 7 or 14 calendar days later, inclusive. If that end time does not exist, use the first valid local instant after the gap; if it occurs twice, use the first occurrence. Compare appointment instants against that window so daylight-saving changes do not shift the chosen wall-clock run time. If the configured run time does not exist on a daylight-saving transition day, skip that week's run. If it occurs twice, run once at the first occurrence. Do not backfill a skipped run. A confirmed appointment overlapping that window makes the client ineligible. A pending request does not. A cancellation removes a confirmed appointment from the current eligibility check, but never erases invitation history.

For each client, require an active profile and a current, verified phone with matching in-person SMS consent, no opt-out, and no previous invitation in `(R - lookahead, R]`. The previous invitation means the last successfully handed-off invitation, not an attempted or failed selection. Reserve one invitation per client and scheduled window with an idempotency key so retries and concurrent runs cannot duplicate it. A missed run does not backfill or send a catch-up burst; the next configured run evaluates current state. Disabling stops future selection. A day/time or lookahead change takes effect at the next run; it does not reset invitation history. If changing from two weeks to one, the one-week interval is measured from the last actual invitation at that next run.

Immediately before handing an invitation to the SMS provider, recheck the client, phone, consent, opt-out, confirmed appointments, and invitation interval against current state. A newly confirmed booking, opt-out, deactivation, or changed phone suppresses the queued text. A newly pending request still does not count as confirmed; the invitation may be sent, but it must not imply that the pending request is confirmed. If a confirmed booking is cancelled before send, the current eligibility check may permit the invitation, subject to the interval. A changed phone requires fresh in-person consent and verification before any invitation to that new number. A queued invitation must not be sent later merely because a disqualifying state subsequently clears; reselect at a future scheduled run. Record suppression without an SMS.

Examples (all run times in the business timezone): a Monday 9:00 a.m. one-week run excludes a client with a confirmed appointment at the following Monday 9:00 a.m.; a pending request at that time alone does not exclude them. If an invitation was sent on Monday at 9:00 a.m., the following Monday's 9:00 a.m. run is the earliest next eligible run, subject to the appointment and consent checks. For a two-week run, a confirmed appointment at 9:00 a.m. on the second following Monday also excludes the client; a later appointment does not. An invitation sent at 9:00 a.m. this Monday first permits another at 9:00 a.m. on the second following Monday. If the run was missed, Tuesday is not a catch-up run. If the owner changes to a two-week lookahead after Monday's invitation, the next Monday run is still inside the two-week repeat interval.

Owner-approved initial invitation (business and service names from the saved business profile): “Smart Scheduling Assistant: Would you like to book a visit for [Service name] with [Business name] in the next [week/two weeks]? Reply with a day and time that works for you, or STOP to opt out.” The program-name prefix stays “Smart Scheduling Assistant” (owner decision, #348), so the text stays generic across businesses and matches the registered sample; the body carries the saved names. The two names come from the saved business profile when the text is built, never from a model or a hard-coded trade word. Without a saved profile the default text cannot be built, so the invitation is not sent (`INVITATION_PROFILE_MISSING`); a custom owner message does not need the profile. A stored copy of the pre-#348 default wording is treated as no custom message. Render the saved lookahead as “week” or “two weeks” so the singular version reads correctly (“in the next week”). Omit the client's name; it is not needed to invite a verified recipient and cannot become stale after queueing. The owner confirmed the current consent disclosure and approved Twilio campaign cover this proactive text; see [A2P_REGISTRATION.md](A2P_REGISTRATION.md#booking-invitations-249-252). A reply enters the existing booking conversation, where availability and owner approval still govern confirmation. The invitation itself creates no booking or hold. The implementation promotes eligible intents into the existing SMS outbox and routes replies through the ordinary inbound booking flow. The approved text is the editable default for one saved message used by both scheduled and immediate invitations. The owner may use any nonblank text up to 500 characters without a STOP-content check; onboarding provides opt-out instructions, while consent and opt-out state are still checked before delivery. Editing the outreach settings after selection suppresses that queued scheduled run; editing the business profile does not, and a queued invitation uses the profile saved when it is delivered (owner decision, #348). The new schedule and invitation delivery gate default to disabled. A provider attempt with uncertain acceptance stays held for operator reconciliation, preventing an automatic duplicate. Separate authorization is required before live sending.


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
