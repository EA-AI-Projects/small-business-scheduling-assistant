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
 * A closed date is one whole-day range. Custom hours close the weekday's normal weekly windows minus
 * the exception's open windows (nothing if that leaves no time). A date with no exception has no ranges.
 */
export function closedRanges(policy: AvailabilityPolicy | null | undefined, day: string): ClosedRange[] {
  const windows = policy?.date_exceptions?.[day];
  if (!policy || !windows) return [];
  if (windows.length === 0) return [{ startMinute: 0, endMinute: MINUTES_PER_DAY, wholeDay: true }];
  // Monday = 0, matching the backend's date.weekday().
  const weekday = (new Date(`${day}T12:00:00Z`).getUTCDay() + 6) % 7;
  const open = windows.map((item) => ({ start: minutes(item.opens), end: minutes(item.closes) }));
  const ranges: ClosedRange[] = [];
  const normal = [...(policy.weekly_windows?.[String(weekday)] ?? [])]
    .map((item) => ({ start: minutes(item.opens), end: minutes(item.closes) }))
    .sort((a, b) => a.start - b.start);
  for (const window of normal) {
    let cursor = window.start;
    for (const item of [...open].sort((a, b) => a.start - b.start)) {
      if (item.end <= cursor || item.start >= window.end) continue;
      if (item.start > cursor) ranges.push({ startMinute: cursor, endMinute: item.start, wholeDay: false });
      cursor = Math.max(cursor, item.end);
    }
    if (cursor < window.end) ranges.push({ startMinute: cursor, endMinute: window.end, wholeDay: false });
  }
  return ranges;
}
