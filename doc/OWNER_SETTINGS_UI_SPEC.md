# Owner Settings UI redesign

Status: published as [parent issue #366](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/366). Product decisions were confirmed on 2026-10-13 and are recorded in [PRD.md](PRD.md#61-scheduling-and-availability).

## Problem Statement

As an owner, Settings is too long and cluttered to scan. The page stacks editable forms under a full Current policy summary that repeats much of the same information. Weekly hours take many rows, making it hard to understand a normal week at a glance or reach other settings quickly.

## Solution

Show four compact Settings cards with concise summaries of saved values: Business details, Availability, Advanced scheduling, and Booking invitations. Selecting a card opens one focused editor using the existing pop-up pattern, including its phone sheet. Each editor has collapsible sections. Availability includes a repeating seven-day view for selecting a weekday and editing its open periods with time inputs. Replace the full Current policy card with the saved-value summaries and a visible list of date exceptions in Availability.

## User Stories

1. As an owner, I want a short Settings page with four recognizable cards, so that I can find the area I need quickly.
2. As an owner, I want each card to summarize saved values, so that I can scan my current configuration without opening every editor.
3. As an owner, I want one editor open at a time, so that I can focus on one settings area.
4. As an owner using a phone, I want the editor to fit in a sheet, so that I can use the same settings on a small screen.
5. As a keyboard user, I want opening and closing an editor to move focus predictably, so that I do not lose my place.
6. As an owner, I want the first section of an editor expanded when it opens, so that the next action is apparent.
7. As an owner, I want to collapse a section without losing my edits, so that I can move between related settings.
8. As an owner, I want Business details grouped into sections, so that its business and service information is easier to scan.
9. As an owner, I want Availability to group Weekly hours, Holidays and booking window, and Date exceptions, so that related rules live together.
10. As an owner, I want to see a repeating seven-day overview in the business timezone, so that I can understand my ordinary open hours at a glance.
11. As an owner, I want each weekday to show its open periods or Closed, so that split shifts and closed days are clear.
12. As an owner, I want to select a weekday from the overview and edit its open and close times with time inputs, so that changes are precise.
13. As an owner, I want to add or remove multiple open periods on a day, so that breaks between open periods remain possible.
14. As an owner, I want the week overview to reflect my unsaved edits, so that I can review the proposed week before saving.
15. As an owner, I want the Settings card to keep showing saved values until a save succeeds, so that I can tell draft from committed policy.
16. As an owner, I want saved date exceptions listed and selectable, so that I can inspect and change an exceptional date or restore normal hours.
17. As an owner, I want regular hours, holidays, and booking horizon to retain a separate save action from date exceptions, so that each change is explicit.
18. As an owner, I want a warning before closing an editor with unsaved changes, so that an accidental close does not discard my work.
19. As an owner, I want an unsaved indicator on an open editor's card, so that I can recognize a pending draft.
20. As an owner, I want a collapsed section containing a validation error to open and receive focus, so that I can find and fix the problem.
21. As an owner, I want rejected or stale saves to retain my draft and explain the problem, so that I can revise or retry without re-entering it.
22. As an owner, I want Advanced scheduling to keep its limits and timezone warning together, so that I can understand the effect of a timezone change.
23. As an owner, I want Booking invitations to separate recurring settings and message editing from Send Invitations Now, so that a one-time action does not crowd routine configuration.
24. As an owner, I want the Load pilot policy action when no policy exists, so that first-time setup still works.

## Implementation Decisions

- Reuse the existing modal pop-up behavior for focus trapping, Escape and outside dismissal, focus return, and the phone sheet. Only one Settings editor is open at once.
- Use accessible collapsible sections whose contents remain mounted while collapsed, preserving in-progress drafts. The first section opens initially.
- Keep existing policy, profile, and outreach API contracts and save boundaries. The Settings redesign changes presentation and interaction, not scheduling semantics.
- The four cards summarize persisted values. The Availability editor may show a live draft preview; summaries update only after a successful save.
- The week is a repeating Monday–Sunday pattern in the business timezone, not a dated month. Its visual strips select a day; native time inputs perform edits. Empty hours mean Closed; multiple same-day open periods remain possible.
- List saved date exceptions inside Availability. Selecting one fills the date exception editor; restoring normal hours removes that exception through the existing save behavior.
- Preserve the existing rejection and stale-version behavior: failed writes do not alter saved summaries and keep the owner's draft. Keep the existing warning for timezone changes.
- A close attempt with a dirty draft offers Keep editing and Discard changes. Collapsing a section does not count as closing and does not discard changes.
- When a save fails client-side validation in a collapsed section, open it, show the existing field errors, and move focus to the error or its field. Expose expanded state and error text to assistive technology.
- Preserve first-time Load pilot policy in Availability. Booking invitations retains its manual send confirmation, eligibility preview, idempotent retry, and existing delivery rules.

## Testing Decisions

- The main test seam is the rendered owner Settings screen with the existing fake owner API, as confirmed by the owner. Test visible behavior and requests, rather than component state or styling internals.
- Cover opening and closing cards, focus return, keyboard operation, section expansion, unsaved-close choices, draft preservation, error reveal and focus, and the phone presentation where the DOM can verify it.
- Cover weekly selection, closed and split-shift days, live draft preview versus saved card summary, regular save, saved exception selection and restoration, and first-time policy setup.
- Cover stale and conflicting saves retaining drafts and persisted summaries; retain the existing form-level tests for policy, profile, and invitation behavior as regression checks.
- Existing policy, profile, outreach, Clients pop-up, and accordion flow tests provide prior art. Run focused frontend checks and type/lint gates; no backend contract test is needed unless implementation changes an API contract.

## Out of Scope

- Changing scheduling rules, API payloads, persistence, or the business timezone model.
- Dragging or resizing time blocks in the week view; selection uses the strips and editing uses time inputs.
- A dated month calendar for weekly hours.
- Changing client-facing booking, the owner Schedule calendar, or invitation delivery policy.

## Further Notes

- The existing Current policy summary is removed only after its useful saved information is represented in the compact cards and Availability exceptions list.
- Approved implementation issues:
  1. [#367 Settings shell and Business details](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/367): four saved-value cards, one pop-up editor at a time, Business details sections, focus return, dirty-close choice, first-time profile behavior, and rendered-flow verification. No blockers.
  2. [#368 Availability](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/368): repeating-week overview and selected-day editing, holidays and booking window, saved exception selection and restore, policy seeding, save and conflict behavior, and rendered-flow verification. Blocked by #367.
  3. [#369 Advanced scheduling](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/369): move limits and timezone into the editor, preserve its warning and save behavior, reveal/focus validation errors in collapsed sections, and rendered-flow verification. Blocked by #367.
  4. [#370 Booking invitations](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/370): recurring settings and message sections, a separate Send Invitations Now section, preserved manual-send flow, and rendered-flow verification. Blocked by #367.
