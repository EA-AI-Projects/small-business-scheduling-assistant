import type { CalendarView } from "@/lib/time";

/** Today, previous/next, range title and Day/Week selector. State lives with the caller. */
export function CalendarControls({ title, view, disabled = false, onToday, onStep, onView }: {
  title: string;
  /** Disable every control, e.g. while the business timezone is still loading. */
  disabled?: boolean;
  view: CalendarView;
  onToday: () => void;
  onStep: (direction: -1 | 1) => void;
  onView: (view: CalendarView) => void;
}) {
  const unit = view === "week" ? "week" : "day";
  return (
    <div className="calendar-controls" role="group" aria-label="Calendar navigation">
      <button type="button" className="pill" disabled={disabled} onClick={onToday}>Today</button>
      <span className="step-buttons">
        <button type="button" className="icon-button" aria-label={`Previous ${unit}`}
          disabled={disabled} onClick={() => onStep(-1)}>‹</button>
        <button type="button" className="icon-button" aria-label={`Next ${unit}`}
          disabled={disabled} onClick={() => onStep(1)}>›</button>
      </span>
      <h2 className="range-title" aria-live="polite">{title}</h2>
      <label className="view-select">
        <span className="visually-hidden">Calendar view</span>
        <select className="pill" value={view} disabled={disabled}
          onChange={(event) => onView(event.target.value === "week" ? "week" : "day")}>
          <option value="day">Day</option>
          <option value="week">Week</option>
        </select>
      </label>
    </div>
  );
}
