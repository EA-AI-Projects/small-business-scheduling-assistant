# Visit reminders

Status: per-client revision approved on 2026-11-01 in [parent #399](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/399). Owner-managed timing and delivery (#400) precedes independent client portal, client SMS and owner SMS slices. This supersedes global timing from #390–#394 / PR #395. GitHub owns implementation status. Deployment and live delivery require separate authorization.

## Problem and solution

The global reminder schedule cannot express individual client timing. Owner and client controls need one shared preference and authorized reads explaining enrollment, timing, blockers and expected upcoming reminders.

Send one optional SMS per confirmed visit, using one versioned enrollment/timing preference per stable client profile. Enforce integer 1–7 calendar days before and minute-precision local time from 08:00 through 18:00 inclusive, in the current business timezone. New profiles start off at one day before, 10 AM. Existing profiles retain enrollment and copy saved business timing once (or one day/10 AM when absent), including unenrolled profiles. Repeated migration preserves individualized choices. Remove global timing from Settings; no active global setting may silently change individual schedules.

## Preference and access

- Owner enrollment records the client's expressed choice. Either owner or client may edit afterward; there is no permanent owner lock. Reminder enrollment is separate from scheduling preferences and SMS consent.
- Partial edits preserve unspecified fields. Timing-only edits preserve enrollment; disabling retains timing. Saving timing cannot re-enroll an opted-out client, record consent, verify a phone or clear STOP.
- Owner and client UI use separate enrollment/timing controls, saved values, business-timezone labeling, range validation, blockers and conflict handling. The owner client card uses a Visit reminders accordion consistent with Profile/Text Consent.
- Portal and client SMS access only the authenticated/verified client's profile. Authorized owners may target a named client; duplicate names and ambiguous identity require clarification before writing.
- Shared authorized reads expose saved enrollment/timing, current blockers and expected upcoming reminder time. Distinguish disabled, blocked and past-time states without guaranteeing delivery.
- Use existing profile versioning, authorization, erasure and generated-contract conventions. Stale concurrent edits cannot overwrite a newer preference; repeated message processing cannot duplicate or reverse a committed change. Existing enrollment-only saves preserve timing.
- Clear texts save immediately with a receipt stating enrollment, days before, local time and business timezone. Explicit timed enrollment ("Remind me two days before at 9 AM") enables reminders and saves timing; timing-only edits preserve enrollment. Ambiguous time (such as "9" without AM/PM) requires clarification before writing.
- An enabled preference is not proof of delivery eligibility. Before sending require current profile/phone/consent/opt-out eligibility and operational delivery gates. Do not add an ingress bypass for opted-out or otherwise ineligible SMS senders. Disabling reminders alone leaves other permitted texts enabled; STOP remains program-wide.

## Timing and lifecycle

- Subtract calendar days from the confirmed visit's local date in the current business timezone, then resolve the configured local send time. Do not subtract fixed elapsed hours. Preserve local calendar dates across DST, month and year boundaries.
- The scheduled local send minute is the selection/delivery window. Work arriving outside that minute expires without catch-up. New enrollment, confirmation or edits after the window starts cannot backfill that minute.
- Select current confirmed visits only; pending requests never qualify. Late confirmation or enrollment creates no catch-up reminder and does not change normal confirmation behavior.
- Timing changes immediately affect only that client's unsent reminders, including existing confirmed visits. Explain when an edit moves the reminder into the past. A changed schedule/timezone never resets attempted or uncertain delivery history.
- Recheck current visit status/start, enrollment/timing, consent, phone verification, STOP, erasure and delivery gates at provider handoff. Stale queued work cannot authorize a send.
- Cancellation or unenrollment suppresses unsent reminders. A pending replacement request retains the original confirmed visit's reminder; approval suppresses the original and evaluates the replacement. The replacement can receive its own future reminder even if the original was sent.

## Delivery and replies

- Reuse scheduled-work, SMS provider, send-claim, consent, erasure and failure-reporting boundaries. Do not introduce a general notification framework.
- A reminder gets at most one provider submission after any attempted or potentially submitted send. Persist the claim before provider submission. Duplicate dispatch, queue redelivery, restarts, callbacks, timing edits and scheduler reruns cannot retry known failures, uncertainty or a post-claim crash. Missed reminders are accepted in preference to duplicates.
- Strictly pre-provider stale suppression may allow a changed future schedule to be evaluated; it must not reset an attempted/uncertain submission. Ordinary non-reminder SMS retains existing retry and lease-recovery behavior. Preference acknowledgments remain ordinary eligible conversation replies.
- Provider acceptance is not handset delivery. Preserve failure/uncertainty accurately; known failures use the existing owner delivery-failure view. Preserve guards for uncertain outcomes.
- Standard reminder wording names the current business, service, full visit date, time and timezone. Exclude private notes and attendance-confirmation prompts. Replies use existing scheduling/cancellation/rescheduling flows. Bare YES changes nothing unless an existing question awaits that answer.

## Verification boundaries

Exercise owner and authenticated client APIs plus existing SMS entry points against shared synthetic persistence, a controlled clock and fake provider. Assert externally saved values, authorization, outbound facts, provider call counts and owner-visible failures. Use sender/outbox and conditional persistence integration checks where an in-memory fake cannot prove races. UI component flows establish rendered interactions, not deployed browser-to-AWS behavior.

| Scenario | Expected behavior |
| --- | --- |
| Existing enrolled/unenrolled profiles, missing business timing, repeated migration, new profiles | Enrollment preserved; business timing copied once with one day/10 AM fallback; individual choices survive repetition; new profiles off at default. |
| Owner save/reload/accordion, partial saves, legacy portal/SMS enrollment saves | Saved values shared; unspecified timing/enrollment retained; global timing absent from Settings. |
| Stale concurrent edits and duplicate messages | Conflict/replay protection without lost updates. |
| Other-client access, duplicate owner name, ambiguous time | Unauthorized access refused; ambiguity clarified before writes. |
| Invalid day/time, valid 1/7 days and 08:00/18:00, minute precision | Valid boundaries saved; invalid input preserves stored values. |
| Two independent client schedules; month/year/DST boundaries | Each follows its own timing in the current business timezone using calendar dates. |
| Disabled, blocked, future and already-past reads | Accurate saved state and expected timing without delivery guarantees. |
| Missing consent, changed/unverified phone, STOP, inactive profile or erasure | No provider submission; applicable restrictions exposed through authorized reads. |
| Confirmed visit versus pending request; late confirmation/enrollment | One eligible reminder for confirmed visit; none for pending or catch-up. |
| Late timing edit, cancellation, unenrollment, STOP or erasure after selection | Stale queued work suppressed before provider handoff. |
| Pending reschedule then approval; replacement after original reminder | Original remains eligible pending approval; approval replaces it; future replacement can get its own reminder. |
| Duplicate selection/dispatch, competing claims or queue redelivery | At most one provider submission. |
| Known rejection, uncertain outcome, crash after claim, missed minute | No retry/backfill; known failures reported; uncertainty not called delivery. |
| Ordinary SMS failures and reminder replies | Existing retry and conversation behavior preserved. |

Run applicable retained backend/frontend, generated-contract and infrastructure checks. Use synthetic data only; no live SMS, deployment or provider spend. These checks do not certify deployed/carrier delivery. Choose behavior/persistence checks, not private-helper tests or test-count targets.

## Delivery slices and exclusions

#400 implements the complete owner-to-delivery slice, compatibility, shared reads and verification. #401 implements the client portal controls on the same shared service. Client SMS reads/changes and owner SMS reads/changes each depend on #400 and include their own behavior verification. The [PRD](PRD.md) owns product rules; the [glossary](../GLOSSARY.md) owns terminology. #390–#394 / PR #395 remains historical evidence for the former global-timing design; those completed tickets are unchanged.

Out of scope: automatic enrollment, bulk/global edits, client-specific timezones, multiple/same-day reminders, email/push/pending-request reminders, attendance confirmation, new cancellation/rescheduling rules, custom reminder copy, retry/catch-up/manual resend, separate dashboards/owner alerts, delivery guarantees, and changes to consent/STOP or ordinary SMS retries. No deployment, live SMS or other consequential operational activation is authorized.
