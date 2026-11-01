# Product Requirements Document: Smart Scheduling Assistant

**Status:** Current accepted requirements
**Reviewed:** 2026-10-14
**Audience:** Business owner, product/engineering collaborators

## 1. Summary

**Product identity (owner decision, 2026-10-05):** The official application name is **Smart Scheduling Assistant**. The approved logo direction is a clear black-and-white, nonhuman face icon with bookworm glasses and freckles, based on the owner's reference image. The final [primary logo](../frontend/public/brand/mark.svg) is for the owner app brand mark; the [simplified favicon](../frontend/public/favicon.svg) is for browser tabs and small icon surfaces. Existing repository and public policy URLs remain stable during the name update.

Build an SMS-first scheduling assistant for a small home-cleaning business. Clients communicate by text to request, reschedule, or cancel cleaning visits. The owner manages availability and approves new booking requests primarily through text. A shared scheduling system is the source of truth, with a simple owner-facing calendar/admin view for visibility and corrections.

The assistant can understand natural-language messages, gather missing details, and present available appointment options. Deterministic scheduling logic—not the language model—validates availability, applies holds, and changes appointment state. New requests require owner approval in the MVP.

## 2. Problem and opportunity

The owner currently coordinates appointments with clients by text, spending substantial time each Sunday negotiating individual schedules. Repeatedly asking clients to choose times creates friction. The product should reduce coordination effort, give clients a clear way to request suitable times, and keep everyone aligned on one schedule.

## 3. Goals

- Reduce the owner’s manual weekly scheduling effort.
- Let clients request, reschedule, and cancel appointments by SMS.
- Provide accurate availability from a shared calendar.
- Keep owner interaction simple and text-first.
- Require owner approval for new bookings during the initial trust-building phase.
- Use one saved service duration for every booking path and support per-appointment overrides by the owner (#339).
- Make the system safe against double bookings and stale availability.

## 4. Non-goals for MVP

- Automatic confirmation of AI-originated booking requests without owner approval.
- Recurring appointments (design should not preclude adding them later).
- Dynamic routing or address-based travel-time optimization.
- Proactive outreach to clients when a cancellation opens a slot.
- Payments, invoicing, staff payroll, or customer acquisition features.
- Native mobile apps or a broader customer-management portal beyond the scoped Calendar and Appointments pages.
- Storing door/entry codes as ordinary AI-generated notes.

## 5. Users and roles

### Owner
The business owner (initially one phone number) manages working availability, receives booking/cancellation notices, and approves or declines requests by natural-language SMS. A minimal authenticated web view is available for schedule review and corrections. An owner account is activated only by invitation or administrator approval; there is no administrator portal in the MVP.

### Client
A known client texts the business number to request a visit, choose from available options, reschedule, cancel, or provide visit-related information. Clients may see all available appointment options through the SMS conversation. The client portal provides separate sign-in, Calendar and Appointments pages for the linked profile.

### Assistant
An AI-assisted SMS interface that identifies intent, gathers required details, explains options, and communicates status. It may not bypass scheduling rules or claim an appointment is confirmed until the system records the applicable state.

## 6. Scope and requirements

### 6.0 Owner and client accounts (#224)

- Owners need administrative approval and a business link; clients need an owner invitation for an existing profile and verified invited email. Separate sign-in, sign-out, recovery and denied-access paths must preserve those links. Authentication alone grants no access. Email verification does not verify a phone or grant SMS consent. See [ACCOUNTS.md](ACCOUNTS.md).
- The client area shares the app header/account menu and offers Calendar and Appointments. It shows only the client's upcoming confirmed visits and unexpired pending requests, clearly distinguishing "Waiting for approval" and "Move request" from Confirmed. Past dates are browsable as an empty calendar; no other client's visits, blocks, notes or busy-time explanations are shown.
- Calendar has Day, Week, Month, Schedule and Year views in the business timezone. Open starts appear in Day/Week only and open an accessible popover with exact local time, timezone and explicit request send. Navigation clamps to the booking horizon; tapping an out-of-horizon day inside a visible period opens Day with a horizon explanation and no availability read. Appointments still lists upcoming bookings, including owner-created visits beyond the calendar horizon.
- A request asks for owner approval and creates the same hold/notifications as SMS. A lost-response retry reuses its key. A taken time creates nothing and offers current alternatives. A pending hold is never shown as expired from the device clock alone; the app rereads authoritative status.
- Cancellation/withdrawal requires a second explicit confirmation showing the exact local time. A move holds the original/version while Calendar offers replacements, creates a pending move, and leaves the original booked until approval. Loading/stale originals cannot turn a move into a new booking. While a move request waits, withdraw it before cancelling or moving the original. Calendar popovers and Appointments share these actions and refresh after writes.
- The application must keep client and owner route permissions separate and enforce role plus record scope on every read/write. Unknown status is "Status unavailable", never assumed confirmed.

### 6.1 Scheduling and availability

- The owner maintains one versioned business profile (#336): display name, short description, support contact, one bookable service (name and description), and a default booking duration. It is the trusted source of business information for the owner UI, client UI, backend, and SMS tools; a model never authors business facts. The pilot has one service and no service catalog. One configured service is required before booking flows rely on the profile: until the owner saves it, SMS replies that the owner will schedule the visit, the client portal reports booking unavailable, and an owner-created request without an explicit duration is refused with `SERVICE_NOT_CONFIGURED` (#339). Edits use an expected version and are audited. The owner edits these in a "Business details" card in regular Settings (#337): it loads the saved values, shows unsaved changes and validation, keeps the owner's edits and shows the newer version after a stale save, and explains that changing the default duration affects future requests while existing appointments keep their duration. Details are in [ARCHITECTURE.md](ARCHITECTURE.md).
- Configure business operating hours; pilot baseline is Monday–Friday, 8:00 a.m. to 5:00 p.m. in `America/Los_Angeles` with daylight saving.
- Use configurable 15-minute start-time increments for the pilot.
- Observed US federal holidays are unbookable by default. The owner can edit holiday closures and add manual exceptions in the admin view. Use the [US Office of Personnel Management schedule](https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/) as the baseline; this is a business closure setting, not a payroll rule. The owner edits the scheduling policy at any time in Settings (#338): weekly hours (a day with none is closed), the holiday setting (closed on observed US federal holidays, or open), date exceptions, the booking horizon, advanced scheduling limits, and the business timezone (with a warning that it changes how dates display and how hours are read). An edit that would invalidate an active booking or pending request is rejected with an explanation, leaves the saved policy unchanged, and keeps what the owner typed.
- Scheduling preferences, business-wide and per client (#379, #380, #381): the owner writes preferred weekdays and visit start-time windows in natural language in a fifth Settings card, "Scheduling preferences". Review changes has the assistant interpret the text and shows concrete weekdays and exact start-time ranges in the business timezone ("mornings" becomes a visible range). Unsupported, unclear, contradictory, or booking-rule-overriding instructions block activation with an explanation; no subset is activated. Preferences may name a client ("Client A prefers Thursday afternoons"): review lists the affected profiles by current name, an ambiguous name needs the owner to pick the profile, an unknown name blocks, and the preference stays with that profile if it is renamed. A client's preference overrides: it replaces the general one on the weekdays it names while general preferences on other weekdays still apply to that client (owner decision, #381). Each portal client sees only their own applicable recommendations. Activate stores exactly the reviewed text and interpretation; the active configuration is shown apart from the draft and is cleared only by an explicit Clear. Preferences are soft: in the client portal's new-booking and reschedule views, otherwise-valid starts that match are labeled neutrally as "Recommended", every valid start stays selectable, and with no match ordinary starts remain. Preferences never change hours, holidays, horizon, duration, buffers, reservations, owner approval, existing visits, pending requests or already-sent offers. SMS offers (#382) offer matching valid starts first within the client's requested window and fill the remaining slots with other valid starts, without telling the client why; an explicit requested window is honored even when nothing in it matches; since #383 they also apply the verified sender's own client-specific preferences with the same precedence as the portal, so two clients can receive different offers from one calendar, a renamed profile keeps its behavior, and a client with no rule gets the business-wide guidance.
- Settings shows five saved-value cards: Business details, Availability, Advanced scheduling, Booking invitations and Scheduling preferences. One editor opens at a time; drafts survive collapsed sections and rejected saves, dirty close requires a choice, and validation errors are revealed with focus. Availability includes a repeating-week editor and saved date exceptions. [OWNER_SETTINGS_UI_SPEC.md](OWNER_SETTINGS_UI_SPEC.md) owns the detailed interaction requirements.
- Configure booking horizon; MVP default is 14 days ahead.
- Configure travel/buffer time between visits; pilot baseline is 30 minutes between visits only, with none before the first or after the last visit of a day.
- Support owner-created unavailable blocks and owner-created appointments.
- Calculate bookable start times using the required visit duration plus buffer and existing bookings/holds.
- Do not offer a start time unless the full visit duration fits within working hours and does not overlap another appointment, hold, or unavailable block.
- Prevent conflicting reservations atomically, including when two requests arrive close together.
- Availability is revalidated when a client selects a time and again when the owner approves.

### 6.2 Client profile and visit duration (one service duration, #339)

- Maintain a client profile with contact number, name, service address, and active state. A client record holds no visit length and no home size.
- The owner enters and verifies client profile fields (name, service address, and active state) in the authenticated calendar. The phone becomes verified when the owner records the client's in-person text consent, after reading the number back; changing the phone clears it (#91). SMS does not collect or change profile fields; it collects only the requested visit date and time. A text from a sender without an active, owner-verified profile and matching consent gets no scheduling reply by SMS, and no scheduling change occurs; texting an unconsented number would need a separate consent decision. The owner decided this in issue #22.
- Every booking path (SMS, the client portal, and owner-created requests and appointments) takes the visit length from the one saved default duration in the business profile, checked against the scheduling policy's maximum visit at the time it is read. The owner cannot save a default longer than the policy maximum, and cannot lower the policy maximum below the saved default. A changed default affects only requests made afterward: existing appointments, pending requests, and active holds keep the duration they were created with, and an offer already quoted at the old length is refused rather than booked at a different length (the client is asked to choose from fresh times).
- Permit an owner to override duration for an individual appointment, including after approval.
- Store the chosen duration on the appointment; later business-profile or policy changes must not silently change existing appointments.
- Notify the client when an approved appointment’s duration or resulting schedule details are changed.

### 6.3 Booking lifecycle and owner approval

- Client requests are created as **Pending approval** and temporarily hold the requested time.
- Default hold duration is 24 hours (one day); owner can configure the duration.
- The owner receives an SMS summary with client, requested time, duration, and a safe way to identify the pending request.
- Owner approval/decline uses exact commands or a bounded model tool loop over current pending requests. The model may resolve any clear wording to one request and version; ambiguous targets require clarification and no write. The backend validates the transition. There is no short-reply allowlist. See [CONVERSATION.md](CONVERSATION.md#owner-approval-and-decline-tool-loop-298).
- Approval converts the pending request into a confirmed appointment and notifies the client.
- Decline releases the hold and informs the client.
- Expiry releases the hold and informs the client that the request expired and they may request another time.
- Owner can inspect and manage pending requests through the minimal admin view.

### 6.4 Cancellation and rescheduling

- Clients may cancel without a cancellation cutoff in the MVP.
- A plain-language cancellation, such as "I can't make Thursday", gets a confirmation question that states the full date and time ("Cancel your Thu Oct 1 at 11:00 AM visit? Reply YES to confirm."). Nothing is cancelled until the client confirms. The owner accepted this extra confirmation text in issue #60.
- "My next appointment" means the earliest upcoming confirmed visit in client-facing SMS. An earlier pending request is called out separately as awaiting approval. "Cancel my next appointment" selects that confirmed visit for the normal second-message confirmation even when other visits exist.
- Cancellation updates the calendar and releases the appointment’s time for future requests.
- Notify the owner by SMS when a client cancels, naming the client, service, date, and time. An owner cancellation notice to the client says that the business cancelled the visit. Routine cancellation texts omit references and repeated help instructions; the 30-minute confirmation lifetime is explained when a late reply expires.
- Do not proactively message other clients about newly available time in the MVP.
- Rescheduling is handled as a new time request tied to the client. Keep the original confirmed appointment until the replacement is approved; a declined or expired replacement leaves it unchanged. Approval swaps them atomically. The owner confirmed this rule in issue #3. A plain-language reschedule ("Can I move Thursday to Friday?") offers replacement times the same way as a new booking; picking one creates the pending replacement.

### 6.4.1 Owner-configured booking invitations (#248, #249)

The owner may enable a scheduled invitation and choose its local day and time and a one- or two-week lookahead. At each run, consider active clients individually. Invite a client only if they have **no confirmed appointment** from the run instant through the end of the selected lookahead. A pending request does not count as a confirmed appointment. This is a targeted eligibility rule; there is no all-clients blast. At most one invitation may be sent to a client in a lookahead-length interval measured from that client's previous invitation. Changing the schedule or lookahead does not erase the previous-invitation limit. See [CONVERSATION.md](CONVERSATION.md#scheduled-booking-invitations-249) for exact boundaries, examples, and send-time checks.

The approved invitation text is the initial value of one owner-editable message in Settings. The owner may save any nonblank text up to 500 characters; the app does not require STOP in this field because the onboarding welcome covers opt-out instructions. The saved text is used by both scheduled and immediate sends. Editing it after a scheduled run was selected suppresses that queued run so later delivery cannot use unexpected copy. Business profile edits do not suppress a queued run: the default text is built from the profile saved at delivery, so a renamed business or service applies to queued invitations (owner decision, #348). The default text keeps the “Smart Scheduling Assistant:” prefix and names the service and business in the body. On 2026-10-06, the owner approved the initial copy and sender label and confirmed current consent and A2P campaign coverage (#252); see [A2P_REGISTRATION.md](A2P_REGISTRATION.md#booking-invitations-249-252). The owner separately controls live deployment and SMS authorization.

The owner may also trigger a one-off run from the same Settings card. It saves an edited message before the run, then uses the saved one- or two-week lookahead and current eligibility checks, including consent and opt-out. Manual runs have no per-client repeat limit; each click has its own idempotency key. The owner previews the eligible count and confirms the send. The manual delivery gate is configured separately from scheduled delivery. The owner plans a separate Twilio campaign for future promotional or unrelated texts.

### 6.4.2 Visit reminders (#399, revising #390–#394)

Visit reminders use one versioned enrollment and timing preference per stable client profile. The accepted design supports owner and client UI and SMS controls, with current SMS restrictions explained without changing consent. #400 implements owner controls and delivery; #401 adds the client portal controls (enrollment, days before and local send time in the business timezone); the remaining #399 children extend client SMS and owner SMS controls. Scheduled delivery and its gate default disabled; implementation does not authorize deployment or live messages.

Accepted full scope (owner decisions, 2026-10-23; per-client revision approved 2026-11-01 in #399):

- Send one SMS reminder per confirmed visit to enrolled clients; pending requests and email reminders are outside this version.
- Maintain one reminder preference per client profile, shared across the owner client-profile UI, the client portal, and the client's SMS conversation. Clients can change it themselves; the owner can record the client's expressed preference.
- New profiles start unenrolled at one calendar day before, 10 AM. Preserve existing enrollment and copy saved business timing for every existing profile, including unenrolled clients; absent business timing defaults to one day/10 AM. Repeated migration preserves individualized choices.
- Reminder enrollment does not grant SMS consent, verify a phone, or override STOP. Existing SMS eligibility gates continue to apply. Disabling reminders alone leaves other permitted program texts enabled; STOP retains its existing program-wide meaning.
- Each profile saves 1–7 integer calendar days before and a minute-precision local time from 08:00 through 18:00 inclusive, in the current business timezone. Remove global timing from Settings. The owner client card uses a Visit reminders accordion consistent with Profile/Text Consent, with separate enrollment and timing controls, saved values, timezone labeling, validation, blockers and conflict handling. Client UI follows the same separate-control behavior.
- Replies use the existing scheduling conversation and cancellation/rescheduling flows. Reminders do not request attendance confirmation. A bare YES changes nothing unless an existing conversation question is awaiting that answer.
- A visit confirmed, or a client enrolled, after that visit's reminder time receives no catch-up reminder. Normal booking confirmation behavior remains in place. Enrollment applies to visits whose reminder time is still ahead.
- Cancellation suppresses unsent reminders. A pending reschedule leaves the original confirmed visit's reminder intact. An approved reschedule replaces the original reminder schedule; the replacement may receive its own reminder at its future reminder time even if the original reminder was already sent.
- Timing changes immediately recalculate only that client's unsent reminders, including existing confirmed visits. Partial edits preserve unspecified values; timing-only edits preserve enrollment; disabling retains timing. Either owner or client may edit afterward, with no permanent owner lock and protection against stale concurrent overwrites and repeated message processing. No timing edit repeats attempted/uncertain submissions or sends catch-up messages when the new reminder time is already past.
- Do not retry failed reminder deliveries in this version. The owner accepts the risk of a missed reminder in exchange for simpler delivery behavior (owner decision, 2026-10-23). Failed sends must not be selected again as new reminders.
- Missed send windows do not trigger catch-up after downtime. An uncertain provider-acceptance outcome does not trigger a resend. The owner accepts missed reminders in preference to duplicate delivery.
- Authorized owner and client reads expose saved enrollment/timing, current blockers, and expected upcoming reminder time, distinguishing disabled, blocked and past-time states without guaranteeing delivery. Clients can access only their own settings; owners may target a named client, clarifying duplicate names before writing.
- Clear reminder texts save immediately with a receipt stating enrollment, days before, time and business timezone. Explicit timed enrollment saves timing and enables reminders; timing-only edits preserve enrollment. Ambiguous times (such as "9" without AM/PM) or identity require clarification before writing. Owner enrollment records the client's expressed choice; saving timing cannot re-enroll an opted-out client.
- Use standard reminder wording naming the business, service, full visit date, time, and timezone. Exclude private notes and attendance-confirmation prompts. Custom reminder-message editing is outside this version.
- Show failed reminders in the existing delivery-failure view. Do not add owner alerts or a separate reminder dashboard.

The [implementation specification](VISIT_REMINDERS_SPEC.md) describes accepted verification boundaries. [Parent #399](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/399) supersedes global timing from #390–#394 / PR #395. Owner-managed timing and delivery (#400) precedes the independent client portal, client SMS and owner SMS slices. The scheduled minute remains the selection/delivery window; delayed work expires without catch-up. These changes authorize neither deployment nor live SMS.

### 6.5 SMS conversation behavior

- Support natural-language requests for availability, booking, cancellation, and rescheduling. The language model resolves relative dates and windows ("tomorrow", "Friday afternoon", "this week") into a candidate day range; the backend checks the booking horizon, holidays, working hours, and existing visits before offering times.
- Each availability reply lists 3–5 real open start times (fewer only when fewer exist) with the full date and time. Offering times writes nothing to the calendar.
- A pending request is created only when the client's reply maps to exactly one offered option, either deterministically or by the model's `request_booking` call validated against the stored offer, such as a bare option number (which takes precedence over a matching clock hour), "11 AM", "the 11 o'clock one", "option 2", or "yes" when a single option was offered. Negated, ambiguous, or unmatched replies ask again and change nothing. The request reply states the full date and time and that owner approval is still required.
- Exact time after an invitation (owner decision 2026-10-07, #272): a reply to a booking invitation that names one exact date and time ("Oct 13 at 1 pm") is treated like any other request. If the time is open, the assistant offers that single time ("… is open for your 2-hour [Service name] visit. Reply YES to request it.") and only the client's acceptance creates the pending request. The model reads that acceptance in any wording (YES, "sure, that works", "the 1 pm") and calls `request_booking`; the backend writes only if the named time is in that client's own unexpired offer. This supersedes the earlier same-day idea of skipping that confirmation: two separate texts must be two separate requests, and an invitation never lets the model write on its own. If the time is not open, the client gets current openings, and a following date-only reply gets that day's openings rather than "What day?".
- An offer or confirmation question stays valid for 30 minutes; an answer after that asks the client to request current times and changes nothing. Any new request replaces the previous offer. The owner set the option count and 30-minute validity in issue #60.
- A verified client can ask about their own schedule in their own words or language ("Do I have bookings this week?", "When are the cleaners coming?", "How many confirmed visits next week?", "¿Tengo citas esta semana?"); the model interprets the question, not keyword rules (owner decision, #241), and the client gets a read-only answer built from their own current confirmed visits and unexpired pending requests, each labeled with its status, full date, and time in the business timezone. A question with no day means all upcoming visits; one that matches nothing says so. Questions about open times keep the availability offer above. A short text that could mean checking a visit or requesting one ("Booking for Friday?") gets one question asking which. A calendar question writes nothing, closes any open offer or confirmation question (the reply says nothing was booked or cancelled), and never shows another client's visits, owner blocks, notes, or profile details (#240). For 30 minutes the client can follow up ("What about next week?", "Just confirmed ones"); each follow-up rereads the calendar and states its dates and statuses, and a bare yes, no, or number after a calendar answer changes nothing. An owner counteroffer the client has not answered stays open: each calendar answer reminds the client of it, and only a plain YES or NO answers it (#241).
- Client texts are handled by one model-led path (#275): the model proposes a typed intent and may phrase the reply, while the backend validates every write against stored state and sends a fixed backend sentence whenever the model fails or its draft does not validate. For the owner (#285) the model writes read-only replies (calendar answers, pending-request summaries, how-to) in full under the same basic check, with the residual risk accepted by the owner on 2026-10-08; offers, decision questions, approvals, declines, and counteroffer outcomes stay fixed backend text, and the model sees the owner's 24-hour thread. The model never sends or books on its own, and an opted-out sender is never sent to the model.
- Exact commands (`BOOK YYYY-MM-DD HH:MM`, `CANCEL REF`, `RESCHEDULE REF to YYYY-MM-DD HH:MM`, `APPROVE REF`, `DECLINE REF`) keep working for clients and the owner.
- Ask concise follow-up questions when client identity, preferred date/time, or other required booking details are missing.
- Offer available times within the 14-day booking horizon and business hours.
- State clearly whether a request is pending, confirmed, declined, cancelled, or expired.
- Do not imply a booking is confirmed before owner approval and successful persistence.
- Handle unsupported requests or uncertainty by asking a clarifying question or routing to the owner.
- Provide opt-out/help handling as required by the selected messaging provider and applicable rules.

### 6.6 Owner SMS workflow

- Send owner notifications for new pending requests and client cancellations.
- Permit natural-language approval/decline replies (see §6.3). The language model interprets owner replies using recent conversation context, so it can tell an approval or decline from calendar or other conversation. When a reply is too ambiguous, the model asks a clarifying question, which changes nothing. The backend validates every approval or decline, and the model never approves or declines by itself. The owner decided this on 2026-10-04 (#174).
- The owner can ask conversational questions about the authoritative calendar, including a weekly breakdown, which clients are scheduled tomorrow, and how many bookings are on Friday. Answers use current calendar and client records, distinguish confirmed visits, pending requests, and unavailable blocks, and state the relevant dates in the business timezone. The assistant asks a focused question when the requested breakdown or date range is unclear and can adapt the answer to follow-up questions. This is an extensible scheduling-assistant goal, delivered through verified question types rather than unrestricted access to business data.
- The owner can propose a different time for a client's request. Before texting the client, the assistant repeats the exact client, date, time, and proposed message for owner confirmation. A client acceptance creates a request that returns to the owner for approval; it does not itself confirm a booking. The original pending request remains pending until the owner approves the accepted replacement, at which point the scheduling service resolves both requests together. Availability and request state are rechecked at each action. The owner confirmed these rules on 2026-10-03. On 2026-10-04 (#176) the owner added that a counteroffer follows the same rules as any client offer: it is open for 30 minutes, an answer after that changes nothing and asks the client to request current times, a new client request replaces it, and the wording is as drafted in #175. The owner-confirmation step is specified in [SCHEDULING_CONTRACTS.md](SCHEDULING_CONTRACTS.md#owner-counteroffer-confirmation-175).
- Send confirmation of the resulting action to the owner.
- Initial owner notification and approval destination is one phone number.
- Owner must be able to block time and review/correct schedule through a simple web admin view; SMS commands may be added if reliable and unambiguous.

### 6.7 Calendar/admin view

Minimal authenticated owner interface:
- The owner calendar provides Day, Week, Month, Schedule and Year views. Use a modern, calendar-first layout with date navigation and a compact menu for owner sections. Schedule lists confirmed visits and pending requests from the selected date, initially covering 30 days with a Load more action. The client portal shares neutral calendar components while keeping client actions and scope separate. The Week view starts on Monday.
- The owner UI uses shades of blue and white, not green and white (owner decision, 2026-10-02, #140).
- Clicking a calendar item opens a pop-up card with its details and actions, as in Google Calendar. Clicking an empty slot opens a card to mark that time unavailable. These cards replace the side "Selected item" and "Block unavailable time" panels (owner decision, 2026-10-02, #140).
- On a phone, the header is a single row that stays at the top of the screen (owner decision, 2026-10-02, #140).
- Today is marked only by the dark-blue date number in the day header; the today column is not highlighted (owner decision, 2026-10-02, #140).
- Clicking a client in the client list opens a "Client details" pop-up with collapsible sections: Profile, Text Consent, Account invitation, and Notes. It replaces the side panels on the Clients page (owner decision, 2026-10-02, #140).
- Client profile email (owner decision, 2026-10-18, #375): Email is an optional field saved in the client profile. The account invitation action uses that saved address instead of a separate email input and shows the destination beside the button. Without a saved email, disable sending and explain "Add an email to the profile first." Unsaved email changes must be saved before sending. Changing or clearing the profile email leaves pending invitations and active account access unchanged; replacing an invited address or identity requires explicitly revoking the old invitation/access and issuing a new invitation. This does not change phone verification or text consent.
- The Account invitation section retains account invitation status and the explicit "Revoke account access" action. Sending is unavailable while an invitation is pending or account access is active; the owner must revoke first to replace either. Multiple client profiles may save the same contact email, including household members sharing an inbox; this does not relax existing account-linking restrictions (owner decision, 2026-10-18, #375).
- The client card's Text Consent section shows whether in-person text consent was recorded for the saved phone, with the recorded date. The "They clearly said yes" checkbox stays unchecked for a new recording and is never prechecked from stored status. Changing the saved phone clears the status. Showing STOP/opt-out status is out of scope (owner decision, 2026-10-05, #217).
- Clicking the date or range title opens a mini month calendar pop-up for jumping to a date (owner decision, 2026-10-02, #140).
- Block-time defaults: clicking an empty slot starts at that half hour and blocks 1 hour, and both can be edited before saving. The Block time button starts at the next half hour today, or at 9:00 AM on other days. A rejected save keeps the card open with the owner's entries (owner decision, 2026-10-02, #140).
- View appointments, pending holds, and unavailable blocks in a calendar/list.
- Create/edit/cancel appointments and unavailable blocks.
- Review and act on pending requests.
- Edit operating days/hours, holiday closures and exceptions, booking horizon, default buffer, hold duration, the service default duration (in the business profile), and client profiles.
- Clearly distinguish pending, confirmed, cancelled, declined, expired, and unavailable time.

### 6.8 Notes and privacy

- Support client-level notes and appointment-level notes as separate records.
- Notes are visible only to authorized business users and are not exposed to other clients.
- The assistant should not silently convert sensitive or ambiguous conversation content into durable notes; the owner should be able to review/edit notes.
- Exclude entry/access codes from this MVP entirely; do not request, extract, store, or send them through the scheduling assistant.
- Delete client-thread SMS message bodies 90 days after the last scheduling exchange. In the owner's own thread with the assistant, delete each stored text (the owner's texts and the assistant's replies) 90 days after that message was sent; later activity does not extend an older message's life. Provider-accepted `SMS_OUT#` send evidence stores exact outbound text for the application-owned conversation history approved in #269 and #270; purge that body under the same client-thread or owner-thread 90-day rule. Other send metadata and outbox items hold no names or message text and keep their current retention. Delete ordinary client/appointment notes 12 months after the last completed visit. For this retention rule, a confirmed appointment counts as a completed visit once its end time passes, unless it was cancelled. If a client has never completed a visit, delete each note 12 months after its creation. Reject a new note when the client's last completed visit was already more than 12 months ago. Keep only minimal consent/opt-out evidence for four years after the last program text, unless a documented legal hold requires longer. The owner approved these pilot periods and edge cases in issues #16 and #21; implementation belongs to the SMS and profile issues.
- The authenticated owner can delete a client and all data associated with that client. Deletion cancels the client's future confirmed appointments and pending requests and releases their reserved time. It removes the profile, notes, appointment history, the client's scheduling-preference rules (the stored preference text is replaced by neutral text from the remaining rules and any stored review binding the client is deleted), SMS conversations, consent and opt-out evidence, and records under legal hold; this explicit owner deletion overrides the ordinary retention and legal-hold rules above. The former client cannot schedule or receive program texts afterward. If they return, the owner must create a new profile and record fresh in-person consent. Deletion may retain only hashed markers for deleted client IDs and already-seen Twilio message IDs to reject ID reuse and delayed retries; these markers contain no phone number, name, message text, notes, or consent. The owner confirmed the deletion rules on 2026-10-03 and the marker exception on 2026-10-04 (#186, #189). Client deletion does not redact or delete stored owner-thread texts that name the erased client; they are removed 90 days after each message was sent (#199, #201).

## 7. Core status model

Appointment/request states:

- `PENDING_APPROVAL` — request accepted for review and its time is held until expiry.
- `CONFIRMED` — owner approved; appointment is scheduled.
- `DECLINED` — owner declined; hold released.
- `CANCELLED` — client or owner cancelled; time released.
- `EXPIRED` — approval window elapsed; hold released.

Unavailable blocks are separate calendar entries, not appointments. Rescheduling preserves the original confirmed appointment until the replacement request is approved.

## 8. Success measures

Measure during a pilot against a baseline period:
- Owner time spent coordinating schedules per week.
- Number of client messages exchanged per completed booking.
- Percentage of requests completed without manual clarification beyond owner approval.
- Booking conflict/double-booking count (target: zero).
- Pending request approval/expiry rates and time to decision.
- Client cancellation and rescheduling completion rates.
- Owner and client satisfaction from brief qualitative feedback.

No numerical improvement target is set until the baseline and pilot cohort are known.

## 9. MVP acceptance criteria

1. An eligible client can text a booking request and receive valid available options for the next 14 days.
2. Choosing a valid option creates a pending request and a 24-hour hold by default.
3. The owner receives a request notification and can approve/decline by natural-language SMS; ambiguity causes clarification, not an accidental action.
4. Approval, decline, or expiry updates the shared calendar and sends the appropriate client notification.
5. Concurrent requests cannot create overlapping appointments or holds.
6. A client can cancel a confirmed appointment by text; the appointment is cancelled, the time becomes available, and the owner receives a text.
7. Duration derives from the saved service default and can be overridden per appointment without changing unrelated appointments.
8. Travel buffer is configurable and defaults to 30 minutes.
9. Owner can review and correct the schedule through a minimal authenticated interface.
10. Client and appointment notes are distinct and access-controlled.

## 10. Assumptions and open questions

- The owner confirmed one bookable crew/resource for the pilot. Multiple crews or staff would change availability modeling.
- The owner confirmed `America/Los_Angeles` with daylight saving, Monday–Friday 8:00 a.m.–5:00 p.m., and observed US federal holiday closures by default. The owner can edit closures and exceptions in the admin view; holiday editing by SMS may be added later if it is reliable and unambiguous.
- The 30-minute travel buffer applies only between visits, with none before the first or after the last.
- One saved service default duration replaces home-size categories. The pilot maximum is 3 hours and starts use configurable 15-minute increments.
- The owner needs a defined method to add unavailable time; the minimal web view is the MVP fallback.
- The owner confirmed replacement-first rescheduling and rejection of conflicting duration increases in issue #3; the original confirmed appointment remains unchanged in either failure case.
- The SMS pilot is limited to senders and recipients in California, US. Use Twilio; a US number is bought and the A2P 10DLC brand and messaging campaign are approved (issue #91), but approval does not authorize live SMS, deployment, or real clients. Consent is obtained in person using the documented [pilot consent process](https://ea-ai-projects.github.io/small-business-scheduling-assistant/sms-consent/) and a private record of the participant's clear yes, method, timestamp, and script version. No initial automated enrollment text is sent before consent. Handle STOP/HELP before live messaging; until launch approval, a number may receive texts only after the owner records in-person consent for it in onboarding (which also verifies the phone), it has not opted out, and `SmsSendEnabled=authorized`; there is no separate deploy-time recipient list (#91). The sender also refuses fictional 555-0100 to 555-0199 numbers.
- When a new client completes onboarding and the owner records in-person text consent, send the first enrollment text using the "Smart Scheduling Assistant" wording on the pilot consent page. Send it once for that client's initial enrollment; Twilio STOP/START re-subscription does not trigger another welcome text.
- Additional owner/staff accounts and notification recipients are deferred; initial owner communication goes to one phone.
- Entry/access codes are excluded from the MVP.

## 11. Deferred work

Recurring appointments, dynamic travel estimates, cancellation-triggered outreach, multi-staff calendars, multiple services, payments and graduated automatic approval require separate product decisions. Current delivery work and pilot progress belong in GitHub issues and the project board, rather than a historical phase checklist here.
