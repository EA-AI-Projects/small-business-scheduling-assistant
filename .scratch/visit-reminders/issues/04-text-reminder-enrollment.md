# 04: Let clients manage reminder enrollment by text

**Status:** Approved and published as https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/394. GitHub owns current work status.

## What to build

Let an eligible verified client enroll or unenroll using clear plain-language texts, such as "remind me about my visits" and "turn off reminders". Save the shared preference and send a short acknowledgment. Verify visibility through the owner preference API without depending on scheduled delivery or the portal UI.

## Acceptance criteria

- [ ] Use the existing client conversation/model path to interpret plain-language enrollment and unenrollment; do not require a new memorized command.
- [ ] Bind writes to the verified sender's profile. Text content cannot nominate another client or business.
- [ ] Clear requests save the explicit preference and receive a short acknowledgment of the committed result. Ambiguous requests ask for clarification without a write.
- [ ] Follow current receipt idempotency and conflict conventions. Duplicate processing does not repeat a committed change or silently overwrite a newer preference.
- [ ] Reminder-specific unenrollment leaves permitted scheduling texts enabled. Program-wide STOP/START retain their current meaning and precedence.
- [ ] Existing consent, profile, phone, and opt-out ingress checks remain authoritative. Reminder requests cannot bypass those checks or count as new consent.
- [ ] The saved choice is the same preference exposed to the owner and client portal; there is no text-specific enrollment record.
- [ ] Plain reminder-preference requests remain distinct from asking about the calendar or booking/cancelling/rescheduling a visit. Existing bare YES handling remains unchanged.
- [ ] Conversation-entry and shared-repository workflow checks prove enrollment, unenrollment, clarification, duplicate receipts, failed/conflicting updates, sender isolation, STOP restrictions, and visibility through owner reads. Use a scripted model and fake SMS boundary rather than live sends.
- [ ] When the portal and delivery slices are available, the parent completion check exercises portal enrollment, one reminder, text unenrollment, and subsequent suppression without creating another implementation ticket.
- [ ] Update current owning conversation references for the behavior implemented by this slice.

## Blocked by

- https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/391
