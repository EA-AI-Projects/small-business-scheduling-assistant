# 01: Configure reminder timing and record client enrollment in the owner UI

**Status:** Approved and published as https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/391. GitHub owns current work status.

## What to build

Let the owner configure the shared reminder schedule in Settings and record a client's expressed enrollment choice on the client profile. Save both through authorized APIs and show their saved state after reload. This slice delivers complete configuration and enrollment controls; scheduled sending is delivered separately.

## Acceptance criteria

- [ ] Settings saves one days-before value from 1–7 and one local time from 8 a.m. through 6 p.m., inclusive; defaults are one day before at 10 a.m. in the business timezone.
- [ ] Server and UI reject out-of-range input without replacing saved values; display saved settings and the business timezone.
- [ ] The client-profile control saves one reminder preference bound to the stable profile identity. Existing and new profiles default off when no preference exists; renaming a client does not move the preference.
- [ ] The owner records the client's expressed choice; the UI distinguishes enrollment from current SMS eligibility and explains applicable delivery restrictions.
- [ ] Enrollment does not record consent, verify a phone, or clear STOP. Other profile data and scheduling preferences retain their existing behavior.
- [ ] Follow existing authorization, version/conflict, generated-contract, and erasure conventions. A stale update cannot silently replace a newer preference. Client deletion removes associated reminder data under existing privacy rules.
- [ ] Owner API and UI flows prove save/reload, valid boundaries, invalid input, defaults for old data, access isolation, eligibility explanations, and conflicts. Validate the persistence/contract boundary using existing patterns.
- [ ] Update current owning references to describe only the configuration behavior implemented by this slice.

## Blocked by

None (can start immediately).
