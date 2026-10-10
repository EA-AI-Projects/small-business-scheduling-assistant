"""Read-only, bounded calendar pages for the verified owner's model tool."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.calendar import CalendarSnapshot, CalendarStatus
from scheduling.domain.client_records import ClientProfile

PAGE_SIZE = 8
MAX_RANGE_DAYS = 31
STATUSES = {
    "confirmed": CalendarStatus.CONFIRMED,
    "pending": CalendarStatus.PENDING_APPROVAL,
    "unavailable": CalendarStatus.UNAVAILABLE,
}


class OwnerCalendarRepository(Protocol):
    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...
    def read_appointment(self, appointment_id: str) -> Appointment | None: ...
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...


def calendar_page(repository: OwnerCalendarRepository, business_id: str,
                  zone: ZoneInfo, now: datetime, first: date, last: date,
                  statuses: tuple[str, ...], offset: int) -> dict[str, Any]:
    """Return one stable-order page from a fresh calendar snapshot.

    Offsets are into the filtered list at the time of this call. Every result is read
    anew; a change between calls may shift offsets, which the model must explain.
    """
    if (last < first or (last - first).days >= MAX_RANGE_DAYS
            or offset < 0 or any(status not in STATUSES for status in statuses)):
        return {"ok": False, "error": "invalid_arguments"}
    wanted = {STATUSES[status] for status in statuses} if statuses else set(STATUSES.values())
    window_start = datetime.combine(first, time.min, tzinfo=zone).astimezone(UTC)
    window_end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    snapshot = repository.read_calendar(business_id)
    events = sorted((event for event in snapshot.events
                     if event.status in wanted and event.occupies_time(now)
                     and event.start_at < window_end and window_start < event.end_at),
                    key=lambda event: (event.start_at, event.event_id))
    labels = {value: key for key, value in STATUSES.items()}
    counts = {label: sum(event.status == status for event in events)
              for label, status in STATUSES.items()}
    # One multi-day item appears on each local day it occupies, while counts
    # describe distinct calendar events. This preserves the prior read rule.
    days = (first + timedelta(days=index) for index in range((last - first).days + 1))
    dated_events = [(day, event) for day in days for event in events
                    if event.start_at < datetime.combine(
                        day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
                    and datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
                    < event.end_at]
    entries: list[dict[str, Any]] = []
    for day, event in dated_events[offset:offset + PAGE_SIZE]:
        start = event.start_at.astimezone(zone)
        end = event.end_at.astimezone(zone)
        entry: dict[str, Any] = {
            "ref": event.event_id[:8], "date": day.isoformat(),
            "status": labels[event.status],
            "start": start.isoformat(), "end": end.isoformat(),
            "start_zone": start.tzname(), "end_zone": end.tzname(),
        }
        if event.status != CalendarStatus.UNAVAILABLE:
            appointment = repository.read_appointment(event.event_id)
            profile = (repository.read_profile(business_id, appointment.client_id)
                       if appointment is not None else None)
            entry["client"] = (profile.name.split()[0][:60]
                               if profile is not None and profile.name.split() else None)
        entries.append(entry)
    following = offset + len(entries)
    return {
        "ok": True, "from": first.isoformat(), "to": last.isoformat(),
        "timezone": zone.key, "statuses": list(statuses) if statuses else list(STATUSES),
        "revision": snapshot.revision, "counts": counts, "total": len(dated_events),
        "offset": offset, "next_offset": following if following < len(dated_events) else None,
        "entries": entries,
    }
