import type { CalendarEvent } from "@/api/types";

import { addDays, dayKey } from "./time";

export interface ScheduleDay {
  date: string;
  events: CalendarEvent[];
}

/** Group only active visits and requests by their start date in the business timezone. */
export function scheduleEvents(events: CalendarEvent[], start: string, days: number, zone: string,
  pendingIds?: ReadonlySet<string>): ScheduleDay[] {
  const end = addDays(start, days);
  const grouped = new Map<string, CalendarEvent[]>();
  for (const event of events) {
    if (event.status !== "CONFIRMED" && event.status !== "PENDING_APPROVAL") continue;
    if (event.status === "PENDING_APPROVAL" && pendingIds && !pendingIds.has(event.event_id)) continue;
    const date = dayKey(event.start_at, zone);
    if (date < start || date >= end) continue;
    const group = grouped.get(date) ?? [];
    group.push(event);
    grouped.set(date, group);
  }
  return [...grouped].sort(([a], [b]) => a.localeCompare(b)).map(([date, items]) => ({
    date, events: items.sort((a, b) => a.start_at.localeCompare(b.start_at) || a.event_id.localeCompare(b.event_id)),
  }));
}
