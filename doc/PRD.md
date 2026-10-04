# Product Requirements Document: Small Business Scheduling Assistant

**Status:** Draft for review  
**Version:** 0.1  
**Date:** 2026-09-25  
**Audience:** Business owner, product/engineering collaborators

## 1. Summary

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
- Support per-client visit duration estimates and per-appointment overrides.
- Make the system safe against double bookings and stale availability.

## 4. Non-goals for MVP

- Automatic confirmation of AI-originated booking requests without owner approval.
- Recurring appointments (design should not preclude adding them later).
- Dynamic routing or address-based travel-time optimization.
- Proactive outreach to clients when a cancellation opens a slot.
- Payments, invoicing, staff payroll, or customer acquisition features.
- A complex client portal or native mobile applications.
- Storing door/entry codes as ordinary AI-generated notes.

## 5. Users and roles

### Owner
The business owner (initially one phone number) manages working availability, receives booking/cancellation notices, and approves or declines requests by natural-language SMS. A minimal authenticated web view is available for schedule review and corrections.

### Client
A known client texts the business number to request a visit, choose from available options, reschedule, cancel, or provide visit-related information. Clients may see all available appointment options through the SMS conversation; there is no client app requirement for MVP.

### Assistant
An AI-assisted SMS interface that identifies intent, gathers required details, explains options, and communicates status. It may not bypass scheduling rules or claim an appointment is confirmed until the system records the applicable state.

## 6. Scope and requirements

### 6.1 Scheduling and availability

- Configure business operating hours; pilot baseline is Monday–Friday, 8:00 a.m. to 5:00 p.m. in `America/Los_Angeles` with daylight saving.
- Use configurable 15-minute start-time increments for the pilot.
- Observed US federal holidays are unbookable by default. The owner can edit holiday closures and add manual exceptions in the admin view. Use the [US Office of Personnel Management schedule](https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/) as the baseline; this is a business closure setting, not a payroll rule.
- Configure booking horizon; MVP default is 14 days ahead.
- Configure travel/buffer time between visits; pilot baseline is 30 minutes between visits only, with none before the first or after the last visit of a day.
- Support owner-created unavailable blocks and owner-created appointments.
- Calculate bookable start times using the required visit duration plus buffer and existing bookings/holds.
- Do not offer a start time unless the full visit duration fits within working hours and does not overlap another appointment, hold, or unavailable block.
- Prevent conflicting reservations atomically, including when two requests arrive close together.
- Availability is revalidated when a client selects a time and again when the owner approves.

### 6.2 Client profile and visit duration

- Maintain a client profile with contact number, name, service address, and estimated visit duration.
- The owner enters and verifies client profile fields (name, service address, and home size/duration) in the authenticated calendar. The phone becomes verified when the owner records the client's in-person text consent, after reading the number back; changing the phone clears it (#91). SMS does not collect or change profile fields; it collects only the requested visit date and time. A text from a sender without an active, owner-verified profile and matching consent gets no scheduling reply by SMS, and no scheduling change occurs; texting an unconsented number would need a separate consent decision. The owner decided this in issue #22.
- Use configurable small, medium, and large home-size categories with pilot default durations of 1, 2, and 3 hours respectively. The current maximum visit is 3 hours; these values must remain editable by the owner.
- Permit an owner to override duration for an individual appointment, including after approval.
- Store the chosen duration on the appointment; later profile changes must not silently change existing appointments.
- Notify the client when an approved appointment’s duration or resulting schedule details are changed.

### 6.3 Booking lifecycle and owner approval

- Client requests are created as **Pending approval** and temporarily hold the requested time.
- Default hold duration is 24 hours (one day); owner can configure the duration.
- The owner receives an SMS summary with client, requested time, duration, and a safe way to identify the pending request.
- Owner can approve or decline using natural-language replies. A plain "yes" or "approve" approves, and "decline" declines, only when exactly one request is pending. With several pending requests, or an ambiguous reply, the assistant lists the pending requests, asks which one, and makes no state change. `APPROVE REF` and `DECLINE REF` always work. The owner decided the plain "yes" rule in issue #60.
- Approval converts the pending request into a confirmed appointment and notifies the client.
- Decline releases the hold and informs the client.
- Expiry releases the hold and informs the client that the request expired and they may request another time.
- Owner can inspect and manage pending requests through the minimal admin view.

### 6.4 Cancellation and rescheduling

- Clients may cancel without a cancellation cutoff in the MVP.
- A plain-language cancellation, such as "I can't make Thursday", gets a confirmation question that states the full date and time ("Cancel your Thu Oct 1 at 11:00 AM visit? Reply YES to confirm."). Nothing is cancelled until the client confirms. The owner accepted this extra confirmation text in issue #60.
- Cancellation updates the calendar and releases the appointment’s time for future requests.
- Notify the owner by SMS when a client cancels.
- Do not proactively message other clients about newly available time in the MVP.
- Rescheduling is handled as a new time request tied to the client. Keep the original confirmed appointment until the replacement is approved; a declined or expired replacement leaves it unchanged. Approval swaps them atomically. The owner confirmed this rule in issue #3. A plain-language reschedule ("Can I move Thursday to Friday?") offers replacement times the same way as a new booking; picking one creates the pending replacement.

### 6.5 SMS conversation behavior

- Support natural-language requests for availability, booking, cancellation, and rescheduling. The language model resolves relative dates and windows ("tomorrow", "Friday afternoon", "this week") into a candidate day range; the backend checks the booking horizon, holidays, working hours, and existing visits before offering times.
- Each availability reply lists 3–5 real open start times (fewer only when fewer exist) with the full date and time. Offering times writes nothing to the calendar.
- A pending request is created only when the client's reply maps deterministically to exactly one offered option, such as "11", "the 11 o'clock one", "option 2", or "yes" when a single option was offered. Negated, ambiguous, or unmatched replies ask again and change nothing. The request reply states the full date and time and that owner approval is still required.
- An offer or confirmation question stays valid for 30 minutes; an answer after that asks the client to request current times and changes nothing. Any new request replaces the previous offer. The owner set the option count and 30-minute validity in issue #60.
- Exact commands (`BOOK YYYY-MM-DD HH:MM`, `CANCEL REF`, `RESCHEDULE REF to YYYY-MM-DD HH:MM`, `APPROVE REF`, `DECLINE REF`) keep working for clients and the owner.
- Ask concise follow-up questions when client identity, preferred date/time, or other required booking details are missing.
- Offer available times within the 14-day booking horizon and business hours.
- State clearly whether a request is pending, confirmed, declined, cancelled, or expired.
- Do not imply a booking is confirmed before owner approval and successful persistence.
- Handle unsupported requests or uncertainty by asking a clarifying question or routing to the owner.
- Provide opt-out/help handling as required by the selected messaging provider and applicable rules.

### 6.6 Owner SMS workflow

- Send owner notifications for new pending requests and client cancellations.
- Permit natural-language approval/decline replies with clarification for ambiguity (see §6.3).
- The language model interprets owner replies using the conversation context, so it can tell an approval or decline from calendar or counteroffer conversation. When a reply is too ambiguous, the model asks a clarifying question. The backend still validates every approval, and the model never approves by itself. The owner decided this on 2026-10-04 (#174).
- The owner can ask conversational questions about the authoritative calendar, including a weekly breakdown, which clients are scheduled tomorrow, and how many bookings are on Friday. Answers use current calendar and client records, distinguish confirmed visits, pending requests, and unavailable blocks, and state the relevant dates in the business timezone. The assistant asks a focused question when the requested breakdown or date range is unclear and can adapt the answer to follow-up questions. This is an extensible scheduling-assistant goal, delivered through verified question types rather than unrestricted access to business data.
- The owner can propose a different time for a client's request. Before texting the client, the assistant repeats the exact client, date, time, and proposed message for owner confirmation. A client acceptance creates a request that returns to the owner for approval; it does not itself confirm a booking. The original pending request remains pending until the owner approves the accepted replacement, at which point the scheduling service resolves both requests together. Availability and request state are rechecked at each action. The owner confirmed these rules on 2026-10-03.
- Send confirmation of the resulting action to the owner.
- Initial owner notification and approval destination is one phone number.
- Owner must be able to block time and review/correct schedule through a simple web admin view; SMS commands may be added if reliable and unambiguous.

### 6.7 Calendar/admin view

Minimal authenticated owner interface:
- The owner UI redesign starts with Day and Week calendar views. Use a modern, calendar-first layout with date navigation and a compact menu for owner sections. Evaluate these two views with the owner before planning Month, Schedule, or Year views. A client-facing view is outside this redesign; keep the owner UI structure extensible for one later. The Week view starts on Monday.
- The owner UI uses shades of blue and white, not green and white (owner decision, 2026-10-02, #140).
- Clicking a calendar item opens a pop-up card with its details and actions, as in Google Calendar. Clicking an empty slot opens a card to mark that time unavailable. These cards replace the side "Selected item" and "Block unavailable time" panels (owner decision, 2026-10-02, #140).
- On a phone, the header is a single row that stays at the top of the screen (owner decision, 2026-10-02, #140).
- Today is marked only by the dark-blue date number in the day header; the today column is not highlighted (owner decision, 2026-10-02, #140).
- Clicking a client in the client list opens a "Client details" pop-up with collapsible sections: Profile, Text Consent, and Notes. It replaces the side panels on the Clients page (owner decision, 2026-10-02, #140).
- Clicking the date or range title opens a mini month calendar pop-up for jumping to a date (owner decision, 2026-10-02, #140).
- Block-time defaults: clicking an empty slot starts at that half hour and blocks 1 hour, and both can be edited before saving. The Block time button starts at the next half hour today, or at 9:00 AM on other days. A rejected save keeps the card open with the owner's entries (owner decision, 2026-10-02, #140).
- View appointments, pending holds, and unavailable blocks in a calendar/list.
- Create/edit/cancel appointments and unavailable blocks.
- Review and act on pending requests.
- Edit operating days/hours, holiday closures and exceptions, booking horizon, default buffer, hold duration, client duration categories, and client profiles.
- Clearly distinguish pending, confirmed, cancelled, declined, expired, and unavailable time.

### 6.8 Notes and privacy

- Support client-level notes and appointment-level notes as separate records.
- Notes are visible only to authorized business users and are not exposed to other clients.
- The assistant should not silently convert sensitive or ambiguous conversation content into durable notes; the owner should be able to review/edit notes.
- Exclude entry/access codes from this MVP entirely; do not request, extract, store, or send them through the scheduling assistant.
- Delete SMS message bodies 90 days after the last scheduling exchange, and ordinary client/appointment notes 12 months after the last completed visit. For this retention rule, a confirmed appointment counts as a completed visit once its end time passes, unless it was cancelled. If a client has never completed a visit, delete each note 12 months after its creation. Reject a new note when the client's last completed visit was already more than 12 months ago. Keep only minimal consent/opt-out evidence for four years after the last program text, unless a documented legal hold requires longer. The owner approved these pilot periods and edge cases in issues #16 and #21; implementation belongs to the SMS and profile issues.
- The authenticated owner can delete a client and all data associated with that client. Deletion cancels the client's future confirmed appointments and pending requests and releases their reserved time. It removes the profile, notes, appointment history, SMS conversations, consent and opt-out evidence, and records under legal hold; this explicit owner deletion overrides the ordinary retention and legal-hold rules above. The former client cannot schedule or receive program texts afterward. If they return, the owner must create a new profile and record fresh in-person consent. Deletion may retain only hashed markers for deleted client IDs and already-seen Twilio message IDs to reject ID reuse and delayed retries; these markers contain no phone number, name, message text, notes, or consent. The owner confirmed the deletion rules on 2026-10-03 and the marker exception on 2026-10-04 (#186, #189).

## 7. Core status model

Appointment/request states:

- `PENDING_APPROVAL` — request accepted for review and its time is held until expiry.
- `CONFIRMED` — owner approved; appointment is scheduled.
- `DECLINED` — owner declined; hold released.
- `CANCELLED` — client or owner cancelled; time released.
- `EXPIRED` — approval window elapsed; hold released.

Unavailable blocks are separate calendar entries, not appointments. Rescheduling should preserve the original confirmed appointment until the replacement request is approved, unless the business explicitly chooses otherwise.

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
7. Duration derives from configured client data and can be overridden per appointment without changing unrelated appointments.
8. Travel buffer is configurable and defaults to 30 minutes.
9. Owner can review and correct the schedule through a minimal authenticated interface.
10. Client and appointment notes are distinct and access-controlled.

## 10. Assumptions and open questions

- The owner confirmed one bookable crew/resource for the pilot. Multiple crews or staff would change availability modeling.
- The owner confirmed `America/Los_Angeles` with daylight saving, Monday–Friday 8:00 a.m.–5:00 p.m., and observed US federal holiday closures by default. The owner can edit closures and exceptions in the admin view; holiday editing by SMS may be added later if it is reliable and unambiguous.
- The 30-minute travel buffer applies only between visits, with none before the first or after the last.
- Small/medium/large categories default to 1/2/3 hours, current maximum 3 hours, and 15-minute start increments. These values are configurable.
- The owner needs a defined method to add unavailable time; the minimal web view is the MVP fallback.
- The owner confirmed replacement-first rescheduling and rejection of conflicting duration increases in issue #3; the original confirmed appointment remains unchanged in either failure case.
- The SMS pilot is limited to senders and recipients in California, US. Use Twilio; a US number is bought and the A2P 10DLC brand and messaging campaign are approved (issue #91), but approval does not authorize live SMS, deployment, or real clients. Consent is obtained in person using the documented [pilot consent process](https://ea-ai-projects.github.io/small-business-scheduling-assistant/sms-consent/) and a private record of the participant's clear yes, method, timestamp, and script version. No initial automated enrollment text is sent before consent. Handle STOP/HELP before live messaging; until launch approval, a number may receive texts only after the owner records in-person consent for it in onboarding (which also verifies the phone), it has not opted out, and `SmsSendEnabled=authorized`; there is no separate deploy-time recipient list (#91). The sender also refuses fictional 555-0100 to 555-0199 numbers.
- When a new client completes onboarding and the owner records in-person text consent, send the first enrollment text using the "Smart Scheduling Assistant" wording on the pilot consent page. Send it once for that client's initial enrollment; Twilio STOP/START re-subscription does not trigger another welcome text. Broader app renaming is separate work (owner decision, 2026-10-03).
- Whether the wife needs access or notifications is deferred; initial owner communication goes to one phone.
- Entry/access codes are excluded from the MVP.

## 11. Suggested delivery phases

1. **Discovery/setup:** record the confirmed scheduling policy, then confirm phone/SMS requirements and pilot clients.
2. **Scheduling foundation:** calendar, client profiles, availability calculation, holds, appointment lifecycle, and owner admin view.
3. **SMS MVP:** client conversations, owner notifications/approval, cancellation, and audit trail.
4. **Pilot and tune:** measure coordination time and errors; refine conversation prompts and operational policies.
5. **Later:** optional recurring schedules, dynamic travel estimates, proactive cancellation fill, multi-staff calendars, and cautiously graduated automatic approval for suitable cases.
