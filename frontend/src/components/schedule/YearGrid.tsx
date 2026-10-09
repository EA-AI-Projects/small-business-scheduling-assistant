import { useEffect, useRef, useState } from "react";

import type { CalendarEvent } from "@/api/types";
import { addMonths, monthGrid } from "@/lib/monthGrid";
import { addDays, localInput, localTime, statusLabel, todayKey } from "@/lib/time";

const WEEKDAYS = [["M", "Monday"], ["T", "Tuesday"], ["W", "Wednesday"], ["T", "Thursday"],
  ["F", "Friday"], ["S", "Saturday"], ["S", "Sunday"]] as const;

function longName(date: string): string {
  return new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC",
  });
}

/** Twelve Monday-first mini calendars backed by the owner calendar snapshot. */
export function YearGrid({ date, events, zone, selectedId, onSelect, onDismiss, onNavigate, onDay }: {
  date: string;
  events: CalendarEvent[];
  zone: string;
  selectedId: string | null;
  onSelect: (id: string, element: HTMLElement) => void;
  onDismiss: () => void;
  onNavigate: (date: string) => void;
  onDay: (date: string) => void;
}) {
  const year = date.slice(0, 4);
  // Keyboard movement may focus a date other than the selected one. A header or date-picker
  // change takes precedence, including when it stays within the same year.
  const [keyboardFocus, setKeyboardFocus] = useState({ selectedDate: date, date });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [now, setNow] = useState(() => new Date());
  const grid = useRef<HTMLDivElement>(null);
  const moveFocus = useRef(false);
  const active = keyboardFocus.selectedDate === date && keyboardFocus.date.startsWith(year)
    ? keyboardFocus.date : date;
  const today = todayKey(zone, now);

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!moveFocus.current) return;
    const target = grid.current?.querySelector<HTMLElement>(`[data-date="${keyboardFocus.date}"]`);
    if (target) {
      moveFocus.current = false;
      target.focus({ preventScroll: true });
    }
  });

  const move = (event: React.KeyboardEvent, current: string) => {
    const weekday = (new Date(`${current}T12:00:00Z`).getUTCDay() + 6) % 7;
    const next = ({
      ArrowLeft: addDays(current, -1), ArrowRight: addDays(current, 1),
      ArrowUp: addDays(current, -7), ArrowDown: addDays(current, 7),
      Home: addDays(current, -weekday), End: addDays(current, 6 - weekday),
      PageUp: addMonths(current, -1), PageDown: addMonths(current, 1),
    } as Record<string, string>)[event.key];
    if (!next) return;
    event.preventDefault();
    moveFocus.current = true;
    setKeyboardFocus({ selectedDate: next.startsWith(year) ? date : next, date: next });
    if (!next.startsWith(year)) onNavigate(next);
  };

  const months = Array.from({ length: 12 }, (_, index) => `${year}-${String(index + 1).padStart(2, "0")}-01`);
  // The owner snapshot covers the whole calendar. An item spanning midnight appears on both
  // dates, while one ending exactly at midnight does not appear on the next date.
  const byDay = new Map<string, CalendarEvent[]>();
  for (const item of events) {
    const start = localInput(item.start_at, zone);
    const end = localInput(item.end_at, zone);
    const last = end.endsWith("T00:00") ? addDays(end.slice(0, 10), -1) : end.slice(0, 10);
    for (let day = start.slice(0, 10); day <= last; day = addDays(day, 1)) {
      if (day.startsWith(year)) byDay.set(day, [...(byDay.get(day) ?? []), item]);
    }
  }

  return (
    <div className="year-calendar" role="region" tabIndex={-1} data-calendar-scroll="" data-popover-clip=""
      aria-label={`Calendar, ${year}`} ref={grid}>
      {months.map((month) => {
        const heading = new Date(`${month}T12:00:00Z`).toLocaleDateString("en-US", {
          month: "long", year: "numeric", timeZone: "UTC",
        });
        return (
          <section className="year-month" key={month} aria-label={heading}>
            <h3>{heading}</h3>
            <div role="grid" aria-label={heading} className="mini-cal-grid">
              <div role="row" className="mini-cal-row">
                {WEEKDAYS.map(([initial, name]) => (
                  <span key={name} role="columnheader" aria-label={name} className="mini-cal-weekday">{initial}</span>
                ))}
              </div>
              {monthGrid(month).map((week) => (
                <div role="row" className="mini-cal-row" key={week[0]?.date}>
                  {week.map((day) => {
                    if (!day.inMonth) return <span key={day.date} role="gridcell" aria-hidden="true" className="year-outside" />;
                    const count = byDay.get(day.date)?.length ?? 0;
                    return (
                      <div key={day.date} role="gridcell" className="year-day-cell">
                        <button type="button" data-date={day.date} tabIndex={day.date === active ? 0 : -1}
                          className={`mini-cal-day year-day${day.date === today ? " today" : ""}`}
                          aria-current={day.date === today ? "date" : undefined}
                          aria-label={`${longName(day.date)}, ${count} ${count === 1 ? "item" : "items"}, open Day view`}
                          onKeyDown={(event) => move(event, day.date)} onClick={() => onDay(day.date)}>
                          <span aria-hidden="true">{day.day}</span>
                        </button>
                        {count > 0 && <button type="button" className="year-count"
                          aria-label={`${expanded === day.date ? "Hide" : "Show"} ${count} ${count === 1 ? "item" : "items"} for ${longName(day.date)}`}
                          aria-expanded={expanded === day.date} onClick={() => {
                            onDismiss();
                            setExpanded(expanded === day.date ? null : day.date);
                          }}>{count}</button>}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
            {expanded?.startsWith(month.slice(0, 7)) && (
              <div className="year-day-detail" role="region" aria-label={`Items for ${longName(expanded)}`}>
                <strong>{longName(expanded)}</strong>
                {(byDay.get(expanded) ?? []).map((item) => (
                  <button type="button" key={item.event_id} data-event-id={item.event_id}
                    data-popover-anchor="" aria-haspopup="dialog"
                    aria-current={item.event_id === selectedId ? "true" : undefined}
                    className={`year-item ${item.status.toLowerCase().split("_")[0]}${item.event_id === selectedId ? " selected" : ""}`}
                    onClick={(click) => onSelect(item.event_id, click.currentTarget)}>
                    <span>{localInput(item.start_at, zone).slice(0, 10) < expanded ? "Continues" : localTime(item.start_at, zone)}</span>
                    <strong>{statusLabel(item.status)}</strong>
                  </button>
                ))}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
