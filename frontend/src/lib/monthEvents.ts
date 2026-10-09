import type { CalendarEvent } from "@/api/types";

import { layoutDay } from "./calendarLayout";
import { monthWeeks } from "./monthGrid";

/** Place the loaded calendar snapshot into every visible local day, including adjacent months. */
export function monthEvents(events: CalendarEvent[], date: string, zone: string): Map<string, CalendarEvent[]> {
  const byDay = new Map<string, CalendarEvent[]>();
  for (const day of monthWeeks(date).flat()) {
    byDay.set(day.date, layoutDay(events, day.date, zone).map(({ event }) => event));
  }
  return byDay;
}
