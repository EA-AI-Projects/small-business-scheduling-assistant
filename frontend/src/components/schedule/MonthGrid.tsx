import { useEffect, useState } from "react";

import type { CalendarEvent } from "@/api/types";
import { monthEvents } from "@/lib/monthEvents";
import { monthWeeks } from "@/lib/monthGrid";
import { dayTitle, localTime, statusLabel, todayKey } from "@/lib/time";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function MonthGrid({ date, events, zone, selectedId, onSelect, onDay }: {
  date: string; events: CalendarEvent[]; zone: string; selectedId: string | null;
  onSelect: (id: string, element: HTMLElement) => void;
  onDay: (date: string) => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(timer);
  }, []);
  const today = todayKey(zone, now);
  const weeks = monthWeeks(date);
  const items = monthEvents(events, date, zone);
  return (
    <div className="month-calendar" role="region" tabIndex={0} data-calendar-scroll="" data-popover-clip=""
      aria-label={`Calendar, ${new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", { month: "long", year: "numeric", timeZone: "UTC" })}`}>
      <div className="month-weekdays" aria-hidden="true">
        {WEEKDAYS.map((day) => <span key={day}>{day.slice(0, 3)}</span>)}
      </div>
      <div className="month-weeks">
        {weeks.map((week) => (
          <div className="month-week" key={week[0]?.date}>
            {week.map((day) => {
              const list = items.get(day.date) ?? [];
              const visible = expanded === day.date ? list : list.slice(0, 2);
              return (
                <div key={day.date} className={`month-day${day.inMonth ? "" : " outside"}`} role="group"
                  aria-label={`${dayTitle(day.date)}, ${list.length} items`}>
                  <button type="button" className={`month-date${day.date === today ? " today" : ""}`}
                    aria-label={`${dayTitle(day.date)}${day.date === today ? ", today" : ""}, open Day view`}
                    aria-current={day.date === today ? "date" : undefined} onClick={() => onDay(day.date)}>
                    {day.day}
                  </button>
                  <div className="month-items">
                    {visible.map((event, index) => (
                      <button key={event.event_id} type="button" data-event-id={event.event_id}
                        data-popover-anchor="" aria-haspopup="dialog" aria-current={event.event_id === selectedId ? "true" : undefined}
                        aria-label={`${localTime(event.start_at, zone)}, ${statusLabel(event.status)}, ${dayTitle(day.date)}`}
                        className={`month-item ${event.status.toLowerCase().split("_")[0]}${event.event_id === selectedId ? " selected" : ""}${index > 0 && expanded !== day.date ? " month-extra" : ""}`}
                        onClick={(click) => onSelect(event.event_id, click.currentTarget)}>
                        <span>{localTime(event.start_at, zone)}</span><strong>{statusLabel(event.status)}</strong>
                      </button>
                    ))}
                    {expanded !== day.date && list.length > 2 && (
                      <button type="button" className="month-more month-more-wide"
                        aria-label={`Show ${list.length - 2} more items for ${dayTitle(day.date)}`}
                        onClick={() => setExpanded(day.date)}>+{list.length - 2} more</button>
                    )}
                    {expanded !== day.date && list.length > 1 && (
                      <button type="button" className="month-more month-more-narrow"
                        aria-label={`Show ${list.length - 1} more items for ${dayTitle(day.date)}`}
                        onClick={() => setExpanded(day.date)}>+{list.length - 1} more</button>
                    )}
                    {expanded === day.date && list.length > 1 && (
                      <button type="button" className="month-more" onClick={() => setExpanded(null)}
                        aria-label={`Show fewer items for ${dayTitle(day.date)}`}>Show less</button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
