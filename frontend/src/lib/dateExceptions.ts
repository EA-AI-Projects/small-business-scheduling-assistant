/** Date exceptions from the policy, as closed time ranges on the Schedule. Federal holidays are not included. */
import type { AvailabilityPolicy } from "@/api/types";

import { MINUTES_PER_DAY } from "./calendarLayout";

export interface ClosedRange {
  startMinute: number;
  endMinute: number;
  /** True when the whole day is closed. */
  wholeDay: boolean;
}

function minutes(time: string): number {
  return Number(time.slice(0, 2)) * 60 + Number(time.slice(3, 5));
}

/**
 * Local-minute ranges that a saved date exception closes on `day` (YYYY-MM-DD, business timezone).
 * A closed date is one whole-day range; custom hours close the time before, between, and after the
 * open windows. A date with no exception has no ranges (normal weekly hours apply).
 */
export function closedRanges(policy: AvailabilityPolicy | null | undefined, day: string): ClosedRange[] {
  const windows = policy?.date_exceptions?.[day];
  if (!windows) return [];
  if (windows.length === 0) return [{ startMinute: 0, endMinute: MINUTES_PER_DAY, wholeDay: true }];
  const open = windows.map((item) => ({ start: minutes(item.opens), end: minutes(item.closes) }))
    .sort((a, b) => a.start - b.start);
  const ranges: ClosedRange[] = [];
  let cursor = 0;
  for (const item of open) {
    if (item.start > cursor) ranges.push({ startMinute: cursor, endMinute: item.start, wholeDay: false });
    cursor = Math.max(cursor, item.end);
  }
  if (cursor < MINUTES_PER_DAY) ranges.push({ startMinute: cursor, endMinute: MINUTES_PER_DAY, wholeDay: false });
  return ranges;
}
