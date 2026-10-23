# 02: Deliver scheduled visit reminders once and expose failures

**Status:** Approved and published as https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/392. GitHub owns current work status.

## What to build

Use the saved schedule and enrollment to deliver one standard SMS reminder for each eligible confirmed visit. Carry the workflow through scheduled selection, durable submission guards, the existing SMS provider boundary, and the existing owner delivery-failure view. Demonstrate it with a controlled clock and fake provider; do not activate live delivery.

## Acceptance criteria

- [ ] Compute reminder dates using calendar-day subtraction and the business timezone, including daylight-saving and month/year boundaries. Pending requests do not qualify.
- [ ] Respect the approved send window. Approved: selection and provider handoff must occur during the scheduled local minute; expired work is skipped without catch-up.
- [ ] Immediately before provider submission, check the current confirmed visit, enrollment, schedule, active profile, verified phone, matching consent, STOP, erasure restrictions, and existing send gates.
- [ ] Late enrollment or confirmation creates no catch-up send. Normal booking confirmations remain unchanged.
- [ ] Cancellation and unenrollment suppress unsent reminders, including already-selected work. A pending reschedule preserves the original reminder; approval replaces it. A replacement with a future reminder time may receive its own reminder even after the original was reminded.
- [ ] Settings/timezone edits recalculate unsent work, suppress stale queued work, and never repeat a sent reminder or backfill a newly past time.
- [ ] Establish durable attempt identity/claims before provider submission. Duplicate scheduler/queue processing, concurrency, expired leases, crashes, uncertain responses, and callbacks cannot create a second provider submission for the same reminder.
- [ ] Known failures and uncertain submissions are not retried or reselected. Missed windows are not backfilled. Existing uncertain-send protections remain intact, and uncertain outcomes are not labeled successful delivery.
- [ ] Known reminder failures are visible in the existing owner text delivery-failure view, including failures before provider acceptance. No new dashboard, owner alerts, or resend control is added.
- [ ] Standard text names the current business, service, full visit date, time, and timezone; excludes private notes; and does not ask for attendance confirmation.
- [ ] Reminder replies use current conversation/cancellation/rescheduling flows. A bare YES does not create a new attendance state and changes nothing unless an existing question awaits that answer.
- [ ] Existing ordinary SMS retry and recovery behavior is preserved; reminder-preference acknowledgments remain ordinary eligible conversation replies.
- [ ] Controlled-clock workflow and sender/queue integration checks prove success, each suppression rule, rescheduling after an original reminder, duplicate processing, failure, uncertainty, crash recovery, content, and ordinary-SMS retry preservation. Use adapter-level conditional persistence checks where fakes cannot prove concurrency behavior.
- [ ] Verify failures through the existing owner API/UI flow. Keep schedule/delivery activation gates disabled by default and run applicable infrastructure checks without deployment or live sends.
- [ ] Update current owning references and include honest limits of synthetic versus deployed/carrier proof.

## Blocked by

- https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/391
