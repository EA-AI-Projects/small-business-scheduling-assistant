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

- Configure business operating hours; MVP default is 8:00 a.m. to 5:00 p.m.
- Configure booking horizon; MVP default is 14 days ahead.
- Configure travel/buffer time between visits; MVP default is 30 minutes.
- Support owner-created unavailable blocks and owner-created appointments.
- Calculate bookable start times using the required visit duration plus buffer and existing bookings/holds.
- Do not offer a start time unless the full visit duration fits within working hours and does not overlap another appointment, hold, or unavailable block.
- Prevent conflicting reservations atomically, including when two requests arrive close together.
- Availability is revalidated when a client selects a time and again when the owner approves.

### 6.2 Client profile and visit duration

- Maintain a client profile with contact number, name, service address, and estimated visit duration.
- Support configurable home-size categories (e.g. small, large, extra-large) with owner-configured default durations. Exact categories and duration mapping are setup decisions.
- Permit an owner to override duration for an individual appointment, including after approval.
- Store the chosen duration on the appointment; later profile changes must not silently change existing appointments.
- Notify the client when an approved appointment’s duration or resulting schedule details are changed.

### 6.3 Booking lifecycle and owner approval

- Client requests are created as **Pending approval** and temporarily hold the requested time.
- Default hold duration is 24 hours (one day); owner can configure the duration.
- The owner receives an SMS summary with client, requested time, duration, and a safe way to identify the pending request.
- Owner can approve or decline using natural-language replies. If the reply is ambiguous or could refer to multiple requests, the assistant asks for clarification and makes no state change.
- Approval converts the pending request into a confirmed appointment and notifies the client.
- Decline releases the hold and informs the client.
- Expiry releases the hold and informs the client that the request expired and they may request another time.
- Owner can inspect and manage pending requests through the minimal admin view.

### 6.4 Cancellation and rescheduling

- Clients may cancel without a cancellation cutoff in the MVP.
- Cancellation updates the calendar and releases the appointment’s time for future requests.
- Notify the owner by SMS when a client cancels.
- Do not proactively message other clients about newly available time in the MVP.
- Rescheduling is handled as a new time request tied to the client. Keep the original confirmed appointment until the replacement is approved; a declined or expired replacement leaves it unchanged. Approval swaps them atomically. The owner confirmed this rule in issue #3.

### 6.5 SMS conversation behavior

- Support natural-language requests for availability, booking, cancellation, and rescheduling.
- Ask concise follow-up questions when client identity, preferred date/time, or other required booking details are missing.
- Offer available times within the 14-day booking horizon and business hours.
- State clearly whether a request is pending, confirmed, declined, cancelled, or expired.
- Do not imply a booking is confirmed before owner approval and successful persistence.
- Handle unsupported requests or uncertainty by asking a clarifying question or routing to the owner.
- Provide opt-out/help handling as required by the selected messaging provider and applicable rules.

### 6.6 Owner SMS workflow

- Send owner notifications for new pending requests and client cancellations.
- Permit natural-language approval/decline replies with clarification for ambiguity.
- Send confirmation of the resulting action to the owner.
- Initial owner notification and approval destination is one phone number.
- Owner must be able to block time and review/correct schedule through a simple web admin view; SMS commands may be added if reliable and unambiguous.

### 6.7 Calendar/admin view

Minimal authenticated owner interface:
- View appointments, pending holds, and unavailable blocks in a calendar/list.
- Create/edit/cancel appointments and unavailable blocks.
- Review and act on pending requests.
- Edit business hours, booking horizon, default buffer, hold duration, client duration categories, and client profiles.
- Clearly distinguish pending, confirmed, cancelled, declined, expired, and unavailable time.

### 6.8 Notes and privacy

- Support client-level notes and appointment-level notes as separate records.
- Notes are visible only to authorized business users and are not exposed to other clients.
- The assistant should not silently convert sensitive or ambiguous conversation content into durable notes; the owner should be able to review/edit notes.
- Avoid storing access codes or other credentials as ordinary notes. If operationally essential, define a separate protected handling approach, access controls, retention, and audit requirements before implementation.
- Define retention and deletion behavior before launch, including message history and client records.

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

- The business operates a single bookable crew/resource initially. If multiple crews or staff need independent calendars, availability modeling changes.
- Business timezone, exact working days, holidays, and exceptions must be configured; only daily hours are currently known.
- The meaning of the 30-minute buffer (between every pair of visits, including first/last visit boundaries) must be confirmed.
- Home-size categories and their estimated durations need owner input.
- The owner needs a defined method to add unavailable time; the minimal web view is the MVP fallback.
- The owner confirmed replacement-first rescheduling and rejection of conflicting duration increases in issue #3; the original confirmed appointment remains unchanged in either failure case.
- SMS provider, number provisioning, consent/opt-out, and jurisdiction-specific compliance are design/deployment decisions.
- Whether the wife needs access or notifications is deferred; initial owner communication goes to one phone.
- Notes involving access codes require a separate security decision; excluded from ordinary AI note-taking.

## 11. Suggested delivery phases

1. **Discovery/setup:** confirm operating days, timezone, duration mapping, buffer semantics, phone/SMS requirements, and pilot clients.
2. **Scheduling foundation:** calendar, client profiles, availability calculation, holds, appointment lifecycle, and owner admin view.
3. **SMS MVP:** client conversations, owner notifications/approval, cancellation, and audit trail.
4. **Pilot and tune:** measure coordination time and errors; refine conversation prompts and operational policies.
5. **Later:** optional recurring schedules, dynamic travel estimates, proactive cancellation fill, multi-staff calendars, and cautiously graduated automatic approval for suitable cases.
