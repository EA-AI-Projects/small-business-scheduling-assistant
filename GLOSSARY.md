# Scheduling language

Terms used when discussing client profiles, account access, schedules, and SMS conversations.

## Language

**Client profile**:
The business's record of a client and their contact details, whether or not the client has account access.

**Profile email**:
The optional contact email saved in a client profile, used as the destination for a new account invitation. It may differ from the email associated with existing account access.

**Account invitation**:
An owner's invitation for a client to gain sign-in access to their existing client profile. It is separate from an invitation to book a visit.
_Avoid_: Booking invitation

**Confirmed visit**:
A service visit the owner has approved and placed on the schedule.
_Avoid_: Pending appointment

**Pending request**:
A proposed visit awaiting the owner's approval. It is not a confirmed visit.
_Avoid_: Confirmed appointment, booked visit

**Weekly hours**:
The business's repeating open periods for each weekday, read in its business timezone. A weekday with no open periods is closed.
_Avoid_: Weekly calendar

**Date exception**:
Hours for one calendar date that override the weekly hours, including a closure or a special opening.
_Avoid_: Weekly hours override

**Scheduling preference**:
The owner's soft guidance about preferred weekdays and visit start-time windows, reviewed and activated from Settings. It marks some valid starts as recommended; it never restricts, adds, or approves a visit. It applies to the whole business or to one client profile, bound to the profile's stable id so a rename does not move it; by default a client's preference replaces the business-wide one on the weekdays it names (pending owner confirmation).
_Avoid_: Scheduling rule, restriction, booking policy

**Recommended start**:
An otherwise-valid start time that matches an active scheduling preference, labeled neutrally "Recommended" for clients. Other valid starts stay selectable.
_Avoid_: Preferred slot (client-facing), best time

**Preference review**:
The assistant's interpretation of the owner's preference text, shown as concrete weekdays and start-time ranges in the business timezone. Only a review without problems can be activated, and only with the exact text it covered.
_Avoid_: Preview, draft rules
