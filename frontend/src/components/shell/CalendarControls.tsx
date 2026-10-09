import { useCallback, useRef, useState } from "react";

import type { CalendarView } from "@/lib/time";
import { PopoverCard } from "../PopoverCard";
import { MiniCalendar } from "./MiniCalendar";

/** Today, previous/next, range title (opens a date picker) and view selector. State lives with the caller. */
export function CalendarControls({ title, shortTitle, announcement = title, view, date, today, scheduleDays, disabled = false, onToday, onStep, onView, onPick }: {
  title: string;
  /** Spoken date range; can be fuller than the compact visible title. */
  announcement?: string;
  /** Compact title for narrow screens; defaults to `title`. */
  shortTitle?: string;
  /** Disable every control, e.g. while the business timezone is still loading. */
  disabled?: boolean;
  view: CalendarView;
  /** Selected date and today's date (YYYY-MM-DD, business timezone). */
  date: string;
  today: string;
  scheduleDays?: number;
  onToday: () => void;
  onStep: (direction: -1 | 1) => void;
  onView: (view: CalendarView) => void;
  /** A day chosen in the date picker. */
  onPick: (date: string) => void;
}) {
  const unit = view === "month" ? "month" : view === "week" ? "week" : view === "schedule" ? `${scheduleDays ?? 30} days` : "day";
  const [picking, setPicking] = useState(false);
  const titleButton = useRef<HTMLButtonElement>(null);
  const getAnchor = useCallback(() => titleButton.current, []);
  const returnFocus = useCallback(() => titleButton.current, []);
  return (
    <div className="calendar-controls" role="group" aria-label="Calendar navigation">
      <button type="button" className="pill" disabled={disabled} aria-label="Today" onClick={onToday}>
        <span className="today-label">Today</span>
        <svg className="today-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
          <rect x="4" y="5" width="16" height="15" rx="2" fill="none" stroke="currentColor" strokeWidth="2" />
          <path d="M4 10h16M8 3v4M16 3v4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          <circle cx="12" cy="15" r="2" fill="currentColor" />
        </svg>
      </button>
      <span className="step-buttons">
        <button type="button" className="icon-button" aria-label={`Previous ${unit}`}
          disabled={disabled} onClick={() => onStep(-1)}>‹</button>
        <button type="button" className="icon-button" aria-label={`Next ${unit}`}
          disabled={disabled} onClick={() => onStep(1)}>›</button>
      </span>
      <h2 className="range-title" title={title}>
        <button ref={titleButton} type="button" className="range-title-button" disabled={disabled}
          aria-haspopup="dialog" aria-expanded={picking} aria-label={`${title}, choose date`}
          // While the picker is open the header is inert, so a press on the title is an outside
          // press: the picker closes and the click never reaches this button, so it cannot reopen.
          onClick={() => setPicking(true)}>
          <span className="range-title-text range-title-long">{title}</span>
          <span className="range-title-text range-title-short" aria-hidden="true">{shortTitle ?? title}</span>
          <span className="range-title-caret" aria-hidden="true" />
        </button>
      </h2>
      <span className="visually-hidden" aria-live="polite">{announcement}</span>
      {picking && (
        <PopoverCard title="Choose a date" getAnchor={getAnchor} returnFocus={returnFocus}
          variant="dropdown" onClose={() => setPicking(false)}>
          <MiniCalendar date={date} today={today} view={view}
            onPick={(next) => { setPicking(false); onPick(next); }} />
        </PopoverCard>
      )}
      <label className="view-select">
        <span className="visually-hidden">Calendar view</span>
        <select className="pill" value={view} disabled={disabled}
          onChange={(event) => onView(event.target.value as CalendarView)}>
          <option value="day">Day</option>
          <option value="week">Week</option>
          <option value="month">Month</option>
          <option value="schedule">Schedule</option>
        </select>
      </label>
    </div>
  );
}
