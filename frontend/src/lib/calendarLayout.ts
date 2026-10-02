/** Pure layout for the Day and Week time grids. Positions are business-local wall-clock minutes. */
import type { CalendarEvent } from "@/api/types";

import { addDays, localInput } from "./time";

export const MINUTES_PER_DAY = 1440;
/** Short items are drawn at least this tall, and overlap is judged on the drawn extent. */
export const MIN_DRAWN_MINUTES = 30;

export interface PlacedEvent {
  event: CalendarEvent;
  /** Minutes after local midnight where the item starts on this day (0 if it began earlier). */
  startMinute: number;
  /** Drawn end in minutes (at least MIN_DRAWN_MINUTES after start, clipped to the day; 1440 if it runs past midnight). */
  endMinute: number;
  /** Zero-based side-by-side column within its overlap group, and the group's column count. */
  lane: number;
  lanes: number;
  continuesBefore: boolean;
  continuesAfter: boolean;
}

function minuteOf(local: string): number {
  return Number(local.slice(11, 13)) * 60 + Number(local.slice(14, 16));
}

/**
 * Items visible on one local calendar day, clipped to it and arranged into side-by-side lanes.
 * Day boundaries are local midnights, so midnight-crossing items appear on both days and an item
 * ending exactly at midnight does not appear on the next day. Positions follow wall-clock labels,
 * so on DST days the column still has 24 hour rows; an item whose wall-clock end is not after its
 * start (the repeated fall-back hour) falls back to its real duration.
 */
export function layoutDay(events: CalendarEvent[], day: string, zone: string): PlacedEvent[] {
  const dayStart = `${day}T00:00`;
  const nextStart = `${addDays(day, 1)}T00:00`;
  const items: Omit<PlacedEvent, "lane" | "lanes">[] = [];
  for (const event of events) {
    const startLocal = localInput(event.start_at, zone);
    const endLocal = localInput(event.end_at, zone);
    const overlapsDay = startLocal < nextStart && endLocal > dayStart;
    const startsToday = startLocal >= dayStart && startLocal < nextStart;
    if (!overlapsDay && !startsToday) continue;
    const continuesBefore = startLocal < dayStart;
    const continuesAfter = endLocal > nextStart;
    const startMinute = continuesBefore ? 0 : minuteOf(startLocal);
    let end = continuesAfter || endLocal === nextStart ? MINUTES_PER_DAY : minuteOf(endLocal);
    if (end <= startMinute) {
      const real = Math.round((Date.parse(event.end_at) - Date.parse(event.start_at)) / 60000);
      end = startMinute + Math.max(real, 0);
    }
    const endMinute = Math.min(MINUTES_PER_DAY, Math.max(end, startMinute + MIN_DRAWN_MINUTES));
    items.push({ event, startMinute, endMinute, continuesBefore, continuesAfter });
  }
  items.sort((a, b) => a.startMinute - b.startMinute || b.endMinute - a.endMinute
    || a.event.event_id.localeCompare(b.event.event_id));

  const placed: PlacedEvent[] = [];
  let group: PlacedEvent[] = [];
  let groupEnd = -1;
  let laneEnds: number[] = [];
  const flush = () => {
    for (const item of group) item.lanes = laneEnds.length;
    placed.push(...group);
    group = [];
    laneEnds = [];
  };
  for (const item of items) {
    if (group.length > 0 && item.startMinute >= groupEnd) flush();
    let lane = laneEnds.findIndex((laneEnd) => laneEnd <= item.startMinute);
    if (lane === -1) lane = laneEnds.length;
    laneEnds[lane] = item.endMinute;
    group.push({ ...item, lane, lanes: 1 });
    groupEnd = Math.max(groupEnd, item.endMinute);
  }
  flush();
  return placed;
}
