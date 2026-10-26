# Optional SMS reminders for confirmed visits

**Status:** Approved and published as https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/390. GitHub owns current work status.

## What to build

Give clients optional SMS reminders for confirmed visits. Reminders default off. The owner records a client's expressed preference on their profile; clients can manage that same preference in the portal or through plain-language texts. One business-wide schedule sends 1–7 calendar days before a visit at a time from 8 a.m. through 6 p.m. in the business timezone, defaulting to one day before at 10 a.m.

Keep existing consent, phone verification, STOP, scheduling, privacy, and send-gate behavior. Do not retry failed or uncertain sends or backfill missed windows. Reuse the existing delivery-failure view. No attendance confirmation, per-client schedules, custom message editing, or live activation is included.

## Published sub-issues

1. #391: Configure reminder timing and record client enrollment in the owner UI. Blocked by: none.
2. #392: Deliver scheduled visit reminders once and expose failures. Blocked by: 1.
3. #393: Let clients manage reminder enrollment in the portal. Blocked by: 1.
4. #394: Let clients manage reminder enrollment by text. Blocked by: 1.

Issues 2, 3, and 4 depend on the shared preference/settings contracts introduced by 1. They do not block each other: portal and text enrollment can be verified through persisted shared state before scheduled delivery exists. Each ticket includes its own API/adapter/UI/workflow proof where applicable; testing is not deferred to a separate final ticket.

## Acceptance criteria

- [ ] All four slices meet their acceptance checks and the agreed reminder specification.
- [ ] A shared-repository workflow demonstrates owner configuration, portal enrollment, a due confirmed visit, one fake-provider submission, text unenrollment, and suppression of a later reminder.
- [ ] Defaults, authorization, consent/STOP restrictions, cancellation/rescheduling, schedule edits, no-retry behavior, failure visibility, and existing conversation behavior are verified.
- [ ] Existing ordinary SMS retries and booking behavior remain unchanged.
- [ ] Applicable retained validation passes; synthetic evidence is distinguished from deployed/carrier evidence.
- [ ] Current product/architecture/conversation references reflect implemented behavior as slices land.
- [ ] No live SMS or deployment occurs without separate authorization.

## Approval

The owner approved the specification, API/conversation/provider and UI test boundaries, scheduled-minute send window, and four-ticket breakdown on 2026-10-23. These files retain the planning record; GitHub issues and the Project board own implementation status.
