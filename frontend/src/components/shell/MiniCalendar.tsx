import { useEffect, useRef, useState } from "react";

import { addMonths, monthGrid, monthStart } from "@/lib/monthGrid";
import { addDays, datesForView, type CalendarView } from "@/lib/time";

const WEEKDAYS = [["M", "Monday"], ["T", "Tuesday"], ["W", "Wednesday"], ["T", "Thursday"],
  ["F", "Friday"], ["S", "Saturday"], ["S", "Sunday"]] as const;

function longName(date: string): string {
  return new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
}

function monthName(date: string): string {
  return new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", {
    month: "long", year: "numeric", timeZone: "UTC" });
}

/**
 * Month grid for jumping to a date: Monday-first weeks, six rows, adjacent-month days muted,
 * today filled, the selected day (Day view) or week (Week view) marked. Rendered inside a
 * PopoverCard. One grid cell is tabbable (roving tabindex): the keyboard-focused date, else the
 * selected date, else today, else the 1st of the shown month.
 */
export function MiniCalendar({ date, today, view, onPick }: {
  date: string;
  today: string;
  view: CalendarView;
  onPick: (date: string) => void;
}) {
  const [month, setMonth] = useState(() => monthStart(date));
  const [focus, setFocus] = useState<string | null>(null);
  // Spoken only when a month button changes the month (focus stays on the button); cell names carry the date during keyboard moves.
  const [announcement, setAnnouncement] = useState("");
  const moveFocus = useRef(true); // focus the active cell on open and after keyboard moves
  const grid = useRef<HTMLDivElement>(null);
  const selected = new Set(view === "month" ? [] : datesForView(date, view));
  const days = monthGrid(month);
  const inShown = (value: string) => value.startsWith(month.slice(0, 7));
  const active = focus && inShown(focus) ? focus : [date, today].find(inShown) ?? month;

  useEffect(() => {
    if (!moveFocus.current) return;
    moveFocus.current = false;
    grid.current?.querySelector<HTMLElement>(`[data-date="${active}"]`)?.focus({ preventScroll: true });
  });

  const go = (next: string) => {
    setAnnouncement("");
    moveFocus.current = true;
    setFocus(next);
    setMonth(monthStart(next));
  };
  const onKey = (event: React.KeyboardEvent) => {
    const weekdayIndex = (new Date(`${active}T12:00:00Z`).getUTCDay() + 6) % 7;
    const moves: Record<string, string> = {
      ArrowLeft: addDays(active, -1), ArrowRight: addDays(active, 1),
      ArrowUp: addDays(active, -7), ArrowDown: addDays(active, 7),
      Home: addDays(active, -weekdayIndex), End: addDays(active, 6 - weekdayIndex),
      PageUp: addMonths(active, -1), PageDown: addMonths(active, 1),
    };
    const next = moves[event.key];
    if (!next) return;
    event.preventDefault();
    go(next);
  };
  const shift = (months: number) => {
    const next = monthStart(addMonths(month, months));
    setFocus(null);
    setMonth(next);
    setAnnouncement(monthName(next));
  };
  const heading = monthName(month);

  return (
    <div className="mini-cal">
      <div className="mini-cal-head">
        <span className="mini-cal-title">{heading}</span>
        <span className="visually-hidden" aria-live="polite">{announcement}</span>
        <button type="button" className="icon-button" aria-label="Previous month" onClick={() => shift(-1)}>‹</button>
        <button type="button" className="icon-button" aria-label="Next month" onClick={() => shift(1)}>›</button>
      </div>
      <div role="grid" aria-label={heading} className="mini-cal-grid" ref={grid} onKeyDown={onKey}>
        <div role="row" className="mini-cal-row">
          {WEEKDAYS.map(([initial, name]) => (
            <span key={name} role="columnheader" aria-label={name} className="mini-cal-weekday">{initial}</span>
          ))}
        </div>
        {days.map((week) => (
          <div role="row" className="mini-cal-row" key={week[0]?.date}>
            {week.map((day) => {
              const classes = ["mini-cal-day", day.inMonth ? "" : "outside",
                day.date === today ? "today" : "", selected.has(day.date) ? "selected" : ""].join(" ").trim();
              return (
                <button key={day.date} type="button" role="gridcell" className={classes} data-date={day.date}
                  tabIndex={day.date === active ? 0 : -1} aria-selected={selected.has(day.date) || undefined}
                  aria-current={day.date === today ? "date" : undefined} aria-label={longName(day.date)}
                  onClick={() => onPick(day.date)}>
                  <span aria-hidden="true">{day.day}</span>
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
