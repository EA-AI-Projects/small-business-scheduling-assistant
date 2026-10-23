# 03: Let clients manage reminder enrollment in the portal

**Status:** Approved and published as https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/393. GitHub owns current work status.

## What to build

Add a client-portal control that reads and changes the signed-in client's shared reminder preference. An enrollment or unenrollment is visible on the owner client profile after reload. This is independently verifiable through persisted preference state; scheduled delivery need not be implemented first.

## Acceptance criteria

- [ ] The portal displays the client's saved reminder preference, with off as the default for missing/new data, and saves explicit enrollment or unenrollment through an authenticated client API.
- [ ] Scope reads and writes to the authenticated profile; a client cannot select another client or business through request input.
- [ ] Show when existing SMS restrictions prevent delivery even if the preference is on. Signing in or enabling reminders does not verify a phone, grant consent, or override STOP.
- [ ] Changes update the same preference used by the owner profile and future scheduled reminders; do not introduce portal-specific enrollment storage.
- [ ] Save/reload, API errors, and stale-write conflicts follow current portal conventions. Do not offer per-client reminder timing.
- [ ] API/UI and shared-repository workflow checks cover both directions of enrollment change, owner/portal visibility, default-off compatibility, eligibility explanations, authorization isolation, and conflicts without requiring live SMS.
- [ ] Update current owning references for the portal behavior implemented by this slice.

## Blocked by

- https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/391
