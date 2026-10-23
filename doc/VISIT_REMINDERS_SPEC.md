# Visit reminders

Status: approved on 2026-10-23 and published as [parent issue #390](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/390), with implementation slices #391–#394. The specification, verification boundaries, scheduled-minute send window, and ticket breakdown were approved together. Not implemented or authorized for live delivery.

## Problem Statement

Clients currently receive scheduling messages but have no optional, scheduled reminder of an upcoming confirmed visit. The owner needs a simple way to offer reminders, while clients need control over whether they receive them through the portal or text conversation.

## Solution

Send one optional SMS reminder for each confirmed visit. Clients start unenrolled. The owner records a client's expressed reminder preference on their profile; clients can change that same preference in the portal or through plain-language texts.

The owner sets one business-wide schedule: 1–7 calendar days before the visit, at a local time from 8 a.m. through 6 p.m., inclusive. The default is one calendar day before at 10 a.m. in the business timezone. Existing SMS eligibility restrictions still apply. Failed, missed, or uncertain sends are not retried or caught up; the owner accepts the risk of missed reminders to keep this version simple.

## User Stories

1. As an owner, I want to configure reminder timing in Settings, so that all enrolled clients follow one business-wide schedule.
2. As an owner, I want a default of one calendar day before at 10 a.m., so that the initial schedule is predictable.
3. As an owner, I want to choose 1–7 calendar days before and a time from 8 a.m. through 6 p.m., so that I can choose suitable advance notice.
4. As an owner, I want reminder times to use the business timezone, so that calendar dates and send times match my schedule.
5. As an owner, I want to record a client's expressed reminder preference on their profile, so that clients without portal access can enroll through me.
6. As a client, I want reminders off until I request them, so that a new feature does not silently enroll me.
7. As an existing client, I want my existing text consent kept separate from reminder enrollment, so that it does not automatically enable reminders.
8. As a signed-in client, I want to enroll and unenroll in the portal, so that I can manage my own preference.
9. As an eligible texting client, I want to request reminders in plain language, so that I do not need to memorize a command.
10. As an eligible texting client, I want to turn reminders off in plain language, so that I can stop them easily.
11. As a client, I want a short acknowledgment of a clear text preference change, so that I know what was saved.
12. As a client, I want an ambiguous text clarified before my preference changes, so that the assistant does not guess.
13. As a client, I want changes through text, the portal, and the owner to affect one shared preference, so that those surfaces agree.
14. As a client, I want disabling reminders to leave other permitted scheduling texts enabled, so that I can still manage visits by text.
15. As a client, I want STOP and existing SMS restrictions respected, so that enabling reminders cannot override my program-wide opt-out.
16. As a client or owner, I want to see when SMS restrictions prevent reminders, so that an enabled preference is not mistaken for delivery eligibility.
17. As a client, I want reminders only for confirmed visits, so that a pending request is not presented as booked.
18. As a client, I want the reminder to name the business, service, full visit date, time, and timezone, so that I can identify the visit.
19. As a client, I want private notes excluded from reminder texts, so that those details are not disclosed.
20. As a client, I want replies to use the existing scheduling conversation, so that I can ask to cancel or reschedule without learning a new workflow.
21. As a client, I want my confirmed visit to remain confirmed without an attendance reply, so that silence does not change the booking.
22. As a client, I want late enrollment or late visit confirmation to avoid an immediate catch-up reminder, so that I do not receive redundant texts.
23. As a client, I want cancellation to suppress an unsent reminder, so that I am not reminded about a cancelled visit.
24. As a client, I want a pending reschedule to preserve the original visit's reminder, so that the still-confirmed visit remains represented.
25. As a client, I want an approved reschedule to replace the original reminder schedule, so that future reminders reflect the replacement visit.
26. As a client, I want a replacement visit to qualify for its own future reminder even if the original was already reminded, so that I can receive notice of the new date.
27. As an owner, I want timing changes to affect unsent reminders without repeating sent ones or producing catch-up sends, so that editing Settings does not cause a burst of texts.
28. As an owner, I want failed reminder sends attempted only once, so that this version avoids retry complexity.
29. As a client, I want duplicate scheduler or queue processing to avoid repeated provider sends, so that one reminder does not become multiple messages.
30. As an owner, I want downtime and uncertain provider outcomes to avoid automatic catch-up or resending, so that recovery does not risk duplicate reminders.
31. As an owner, I want failed reminders visible in the existing text delivery-failure view, so that I can inspect failures without a new dashboard or alerts.

## Implementation Decisions

### Preference and access

- Persist one reminder preference bound to the stable client profile identity. An absent preference means off, including for existing profiles. Reminder enrollment is distinct from the existing scheduling preference concept.
- The owner client-profile control records the client's expressed choice. Portal changes are scoped to the authenticated client's own profile; the text path uses the verified sender's profile, never an identity supplied in message text.
- Use existing authorization, profile versioning/conflict handling, API contract generation, and client erasure conventions. A concurrent edit must not silently overwrite a newer preference. Repeated processing of one text must not reverse or duplicate a committed update.
- A saved on preference is not proof of delivery eligibility. Owner and portal controls show the preference and explain applicable SMS restrictions. Existing text ingress restrictions remain authoritative; do not add a reminder-enrollment bypass for opted-out or otherwise ineligible senders.
- Reminder enrollment neither records fresh SMS consent nor verifies a phone nor clears STOP. Before delivery, require current profile/phone/consent/opt-out eligibility and existing operational send gates.

### Timing and lifecycle

- The owner's Settings control saves one shared days-before value and one local send time. Enforce the accepted ranges on the server and show saved values and validation errors in the UI.
- Compute the reminder date by subtracting calendar days from the visit's local date in the current business timezone, then resolve the configured local send time. Do not subtract a fixed number of elapsed hours. Daylight-saving changes must preserve local calendar-day semantics.
- Use the existing minute-based scheduled-work pattern. Approved operational boundary: the scheduled local send minute is the selection/delivery window; delayed work outside that minute expires instead of backfilling. The owner approved this operational boundary with the specification and ticket breakdown.
- Only current confirmed visits whose reminder time remains eligible qualify. Pending requests never qualify. Late confirmation or enrollment does not create catch-up work.
- At send handoff, recheck current visit status and time, reminder preference, schedule, SMS eligibility, and erasure restrictions. Queued work cannot authorize a stale reminder.
- Cancellation or unenrollment suppresses unsent reminders. A pending replacement request leaves the original confirmed visit's reminder intact. Approval suppresses the original reminder and evaluates the replacement under the current schedule.
- A replacement may receive its own future reminder even after the original reminder was sent. A changed schedule does not reset delivery history or cause a sent reminder to be repeated.
- Timing edits apply to unsent reminders and use current settings at handoff; a newly past reminder time is skipped. Business-timezone changes use the same recalculation and no-catch-up principles.

### Delivery and replies

- Reuse existing scheduled-work, SMS provider, send-claim, consent, erasure, and failure-reporting boundaries where feasible. Introduce only the reminder-specific selection and delivery policy needed for this feature.
- The existing ordinary SMS sender retries failures and may recover expired send leases. Reminders need a distinct no-retry policy: duplicate dispatch, queue redelivery, worker restarts, callbacks, settings edits, or scheduler reruns must not create another provider submission for a reminder already attempted or potentially submitted. Other text categories retain their current retry behavior.
- Persist enough delivery identity and outcome evidence to prevent repeat attempts for the same confirmed visit, while allowing a separately approved replacement visit's reminder. Establish the claim before calling the provider; a crash or uncertain response after that claim must not authorize a resend. Missed messages are an accepted consequence.
- Provider acceptance is not proof of handset delivery. Preserve known failure and uncertain outcomes accurately; do not label an uncertain send as successfully delivered or conclusively rejected.
- Do not retry known failures, resend uncertain submissions, or backfill missed send windows. Record known failures through the existing owner failure view. Retain existing protections for send claims with uncertain outcomes.
- Use standard wording with the current business name, service, full visit date, visit time, and timezone. Do not include private notes or ask the client to confirm attendance. Custom message editing is excluded.
- Reminder replies use existing conversation state and scheduling operations. A bare YES changes nothing unless an existing question awaits that answer. Cancellation and rescheduling retain their current confirmation and approval rules.
- Keep reminder preference acknowledgments distinct from visit reminders: they use existing eligible conversation-reply behavior, not a new reminder retry policy for unrelated texts.

## Testing Decisions

Approved verification boundaries:

- **Primary workflow boundary:** exercise the owner and authenticated client APIs plus the existing SMS conversation entry point against one shared synthetic repository, a controlled clock, scheduled reminder processing, and a fake provider. Assert saved settings, cross-surface preference visibility, calendar behavior, provider call count, outbound content, and owner-visible failure records. This follows the retained portal/SMS cross-path and scheduled booking-invitation workflow checks.
- **Provider/queue boundary:** use the existing outbox and sender integration patterns to check duplicate dispatch, competing claims, queue redelivery, known rejection, uncertainty, crash recovery, and callbacks. Prove at most one provider submission for a reminder and preserve the existing retry behavior for ordinary messages. Adapter checks should cover conditional persistence where an in-memory fake cannot prove concurrency behavior.
- **UI flow boundary:** extend the existing Settings, client-details, client-portal, and delivery-failure component flows. Exercise save, reload, validation, eligibility explanation, authorization errors, and conflict handling. These checks establish rendered interactions, not deployed browser-to-AWS behavior.

Test external behavior rather than private helpers or implementation-shaped units. Reuse existing seams rather than adding a general notification framework.

Acceptance scenarios:

| Scenario | Expected behavior |
| --- | --- |
| Existing profile without a reminder field; newly created profile | Reminders off. |
| Owner, portal, and text each change enrollment | All surfaces read one saved preference; another client cannot change or read it. |
| Clear enrollment/unenrollment text; ambiguous text; duplicate receipt | One acknowledged clear change; clarification without a write for ambiguity; no duplicate committed update. |
| Preference on with missing consent, changed/unverified phone, STOP, inactive profile, or erasure | No provider submission; applicable restrictions explained on authorized UI surfaces. |
| Reminder preference turned off | Future unsent reminders suppressed; other eligible scheduling texts remain available. |
| Days-before values 1 and 7; times 8 a.m. and 6 p.m.; out-of-range values | Valid boundaries saved; invalid values rejected without replacing saved settings. |
| Visits across month/year boundaries and daylight-saving changes | Reminder follows local calendar-day and business-time semantics. |
| Due confirmed visit versus pending request | One reminder for the confirmed visit; none for the pending request. |
| Confirmation or enrollment after reminder time | No catch-up reminder; normal confirmation behavior unchanged. |
| Cancellation, unenrollment, STOP, or profile erasure after selection | Stale queued reminder suppressed before provider handoff. |
| Pending reschedule followed by approval | Original remains eligible while pending; approval suppresses original and evaluates replacement. |
| Replacement has a future reminder time after original was already reminded | Replacement can receive one reminder. |
| Settings/timezone change before send; new time already past; reminder already sent | Recalculate unsent work; no catch-up or repeat send. |
| Duplicate selection/dispatch or concurrent worker execution | At most one provider submission for that reminder. |
| Provider rejects; provider acceptance uncertain; worker crashes after claim | No automatic second submission. Known failures appear in existing failure reporting; uncertainty is not represented as confirmed delivery. |
| Missed send minute or work arrives after its window | Skip without backfill. |
| Reminder content and reply | Required visit details present, notes absent; existing reply flows work; no new attendance-confirmation state. |
| Ordinary non-reminder SMS failure | Existing retry and recovery behavior unchanged. |

Synthetic checks require no real phone numbers, live SMS, deployment, or provider spend. They do not certify live infrastructure or carrier delivery. Run applicable retained backend/frontend, generated-contract, and infrastructure validation checks when implementing; do not add tests solely to mirror internal helpers.

## Out of Scope

- Email, push, or pending-request reminders.
- Automatic enrollment, per-client timing, multiple reminders per visit, and same-day reminder configuration.
- Attendance confirmation, new cancellation rules, or a new rescheduling workflow.
- Custom reminder message editing.
- Retry/backoff, catch-up runs, automatic uncertain-send reconciliation, or manual resend controls.
- Owner reminder alerts, a separate reminder dashboard, and delivery guarantees.
- Replacing program-wide SMS consent/STOP behavior or redesigning ordinary notification retries.
- Deployment, live SMS activation, and other consequential external actions without separate authorization.

## Further Notes

- The PRD owns the accepted product requirements; the glossary owns shared terminology. This approved specification adds implementation and verification detail without asserting the feature already exists.
- Use the repository's GitHub issue and Project workflow. Use `agent-owned` as the next-owner label, `ready-for-agent` as the separate readiness classification, and `blocked` while a named dependency remains unfinished.
- The owner approved publication on 2026-10-23. The parent specification is issue #390; owner controls are #391; scheduled delivery is #392; portal enrollment is #393; and text enrollment is #394. Each of #392–#394 depends only on #391. GitHub issues and the Project board own implementation status.
- No new ADR is proposed: the reminder choices can be captured in requirements and this specification without introducing another architecture record.
