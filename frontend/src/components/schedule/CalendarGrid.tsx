import { useEffect, useRef, useState } from "react";

import type { AvailabilityPolicy } from "@/api/types";
import { itemKind, type CalendarItem } from "@/calendar/item";
import { closedRanges } from "@/lib/dateExceptions";
import { layoutDay, MINUTES_PER_DAY, type PlacedEvent } from "@/lib/calendarLayout";
import { dayTitle, localInput, localTime, statusLabel, todayKey } from "@/lib/time";

/** Pixels per hour; also set as a CSS variable so the hour lines match. */
const HOUR_PX = 56;
/** The grid is a full 24 hours and opens scrolled to this hour. */
const OPEN_HOUR = 7;
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);

function hourLabel(hour: number): string {
  if (hour === 0) return "";
  return `${hour % 12 === 0 ? 12 : hour % 12} ${hour < 12 ? "AM" : "PM"}`;
}

function minuteLabel(minute: number): string {
  if (minute >= MINUTES_PER_DAY) return "midnight";
  const hour = Math.floor(minute / 60);
  return `${hour % 12 === 0 ? 12 : hour % 12}:${String(minute % 60).padStart(2, "0")} ${hour < 12 ? "AM" : "PM"}`;
}

function eventLabel(placed: PlacedEvent, zone: string, day: string): string {
  const { event } = placed;
  const extra = `${placed.continuesBefore ? ", continues from previous day" : ""}${
    placed.continuesAfter ? ", continues to next day" : ""}`;
  return `${localTime(event.start_at, zone)} to ${localTime(event.end_at, zone)}, ${statusLabel(event.status)}, ${
    dayTitle(day)}${extra}`;
}

export function CalendarGrid({ days, events, policy, zone, selectedId, onSelect, onSlotPress }: {
  days: string[]; events: CalendarItem[]; policy?: AvailabilityPolicy | null; zone: string; selectedId: string | null;
  onSelect: (id: string, element: HTMLElement) => void;
  /** A press on an empty part of a day column (not on an item, heading, or the gutter). */
  onSlotPress: (column: HTMLElement, x: number, y: number) => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const [now, setNow] = useState(() => new Date());
  const first = days[0];
  const count = days.length;

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(timer);
  }, []);
  // Open near working hours whenever the range changes.
  useEffect(() => {
    if (scroller.current) scroller.current.scrollTop = Math.max(0, (OPEN_HOUR - 0.5) * HOUR_PX);
  }, [first, count]);

  const today = todayKey(zone, now);
  const nowMinute = (() => {
    const local = localInput(now.toISOString(), zone);
    return Number(local.slice(11, 13)) * 60 + Number(local.slice(14, 16));
  })();

  return (
    <div className="cal-scroll" ref={scroller} tabIndex={0} role="region" data-calendar-scroll="" data-popover-clip=""
      aria-label={count > 1 ? `Calendar, week of ${dayTitle(first ?? "")}` : `Calendar, ${dayTitle(first ?? "")}`}>
      <div className={`cal-grid ${count > 1 ? "week" : "day"}`}
        style={{ ["--cal-days" as string]: count, ["--hour-px" as string]: `${HOUR_PX}px` }}>
        <div className="cal-head">
          <div className="cal-corner" />
          {days.map((day) => {
            const date = new Date(`${day}T12:00:00Z`);
            return (
              <div key={day} className={`cal-head-day${day === today ? " today" : ""}`}
                aria-label={day === today ? `${dayTitle(day)}, today` : dayTitle(day)}>
                <span className="cal-dow">{date.toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" })}</span>
                <span className="cal-date">{date.getUTCDate()}</span>
              </div>
            );
          })}
        </div>
        <div className="cal-body">
          <div className="cal-gutter" aria-hidden="true">
            {HOURS.map((hour) => <div key={hour} className="cal-hour-label">{hourLabel(hour)}</div>)}
          </div>
          {days.map((day) => (
            <div key={day} className={`cal-col${day === today ? " today" : ""}`} role="group"
              data-slot-column="" data-day={day}
              // Items are buttons inside the column, so only a press on the column itself is empty.
              onClick={(click) => {
                if (click.target === click.currentTarget) onSlotPress(click.currentTarget, click.clientX, click.clientY);
              }}
              aria-label={day === today ? `${dayTitle(day)}, today` : dayTitle(day)}>
              {closedRanges(policy, day).map((range) => (
                <div key={`closed-${range.startMinute}`} className="cal-closed" role="note" data-date-exception=""
                  aria-label={`${range.wholeDay ? "Closed (date exception)" : "Closed (date exception hours)"}, ${
                    minuteLabel(range.startMinute)}–${minuteLabel(range.endMinute)}, ${dayTitle(day)}`}
                  style={{ top: `${(range.startMinute / MINUTES_PER_DAY) * 100}%`,
                    height: `${((range.endMinute - range.startMinute) / MINUTES_PER_DAY) * 100}%` }}>
                  <span>{range.wholeDay ? "Closed (date exception)" : "Closed (date exception hours)"}</span>
                </div>
              ))}
              {layoutDay(events, day, zone).map((placed) => (
                <EventBlock key={placed.event.event_id} placed={placed} zone={zone} day={day}
                  selected={placed.event.event_id === selectedId} onSelect={onSelect} />
              ))}
              {day === today && (
                <div className="cal-now" aria-hidden="true"
                  style={{ top: `${(nowMinute / MINUTES_PER_DAY) * 100}%` }} />
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function EventBlock({ placed, zone, day, selected, onSelect }: {
  placed: PlacedEvent; zone: string; day: string; selected: boolean; onSelect: (id: string, element: HTMLElement) => void;
}) {
  const { event, startMinute, endMinute, lane, lanes } = placed;
  const kind = itemKind(event);
  return (
    <button type="button" aria-label={eventLabel(placed, zone, day)} aria-current={selected ? "true" : undefined}
      aria-haspopup="dialog" data-popover-anchor="" data-event-id={event.event_id}
      className={`cal-event ${kind}${selected ? " selected" : ""}${placed.continuesBefore ? " cont-before" : ""}${
        placed.continuesAfter ? " cont-after" : ""}`}
      onClick={(click) => onSelect(event.event_id, click.currentTarget)}
      style={{
        top: `${(startMinute / MINUTES_PER_DAY) * 100}%`,
        height: `${((endMinute - startMinute) / MINUTES_PER_DAY) * 100}%`,
        left: `${(lane / lanes) * 100}%`,
        width: `${100 / lanes}%`,
      }}>
      <strong>{statusLabel(event.status)}</strong>
      <span>{localTime(event.start_at, zone)}–{localTime(event.end_at, zone)}</span>
    </button>
  );
}
