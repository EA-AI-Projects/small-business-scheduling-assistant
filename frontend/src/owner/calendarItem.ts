import type { CalendarItem } from "@/calendar/item";
import type { CalendarEvent } from "@/api/types";

/** Owner calendar events map to neutral items by dropping the owner-only fields. */
export function toCalendarItem(event: CalendarEvent): CalendarItem {
  return { event_id: event.event_id, start_at: event.start_at, end_at: event.end_at, status: event.status };
}
