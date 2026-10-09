import type { CalendarEvent } from "@/api/types";

import { layoutDay, type PlacedEvent } from "./calendarLayout";
import { monthWeeks } from "./monthGrid";
import { localTime } from "./time";

/** Place the loaded calendar snapshot into every visible local day, including adjacent months. */
export function monthEvents(events: CalendarEvent[], date: string, zone: string): Map<string, PlacedEvent[]> {
  const byDay = new Map<string, PlacedEvent[]>();
  for (const day of monthWeeks(date).flat()) {
    byDay.set(day.date, layoutDay(events, day.date, zone));
  }
  return byDay;
}

/** The start shown on a particular day, rather than the original start of an overnight item. */
export function monthChipTime(placed: PlacedEvent, zone: string): string {
  return placed.continuesBefore ? "12:00 AM" : localTime(placed.event.start_at, zone);
}
