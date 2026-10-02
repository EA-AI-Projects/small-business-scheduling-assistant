import type { CalendarView } from "@/owner/OwnerContext";

/** Today, previous/next, range title and Day/Week selector. State lives with the caller. */
export function CalendarControls({ title, view, onToday, onStep, onView }: {
  title: string;
  view: CalendarView;
  onToday: () => void;
  onStep: (direction: -1 | 1) => void;
  onView: (view: CalendarView) => void;
}) {
  const unit = view === "week" ? "week" : "day";
  return (
    <div className="calendar-controls" role="group" aria-label="Calendar navigation">
      <button type="button" className="pill" onClick={onToday}>Today</button>
      <span className="step-buttons">
        <button type="button" className="icon-button" aria-label={`Previous ${unit}`}
          onClick={() => onStep(-1)}>‹</button>
        <button type="button" className="icon-button" aria-label={`Next ${unit}`}
          onClick={() => onStep(1)}>›</button>
      </span>
      <h2 className="range-title" aria-live="polite">{title}</h2>
      <label className="view-select">
        <span className="visually-hidden">Calendar view</span>
        <select className="pill" value={view}
          onChange={(event) => onView(event.target.value === "week" ? "week" : "day")}>
          <option value="day">Day</option>
          <option value="week">Week</option>
        </select>
      </label>
    </div>
  );
}
