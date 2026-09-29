import { useState } from "react";

import type { CalendarEvent } from "@/api/types";
import { datesForView, dayKey, dayTitle, localTime, statusLabel } from "@/lib/time";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

import { SectionHeading } from "../Workspace";
import { BlockForm } from "./BlockForm";
import { EventDetail } from "./EventDetail";

export function ScheduleTab() {
  const { data, loaded, stamp, refresh, notify } = useOwner();
  const [date, setDate] = useState("");
  const [view, setView] = useState<"day" | "week">("day");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [handledStamp, setHandledStamp] = useState(stamp.version);

  // Default to today in the business timezone once the policy's zone is known.
  if (loaded && !date) setDate(dayKey(new Date().toISOString(), data.zone));

  // A normal refresh clears the selection; a conflict refresh keeps it if still present.
  if (stamp.version !== handledStamp) {
    setHandledStamp(stamp.version);
    if (!stamp.preserveSelection) setSelectedId(null);
  }

  const events = data.calendar?.events ?? [];
  const selected = selectedId ? events.find((event) => event.event_id === selectedId) : undefined;

  return (
    <>
      <SectionHeading eyebrow="CALENDAR" title="Schedule">
        <button type="button" onClick={() => refresh().catch((error: unknown) => notify(errorMessage(error), true))}>
          Refresh
        </button>
      </SectionHeading>
      <div className="toolbar card">
        <label>Start date <input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label>
        <label>View{" "}
          <select value={view} onChange={(event) => setView(event.target.value === "week" ? "week" : "day")}>
            <option value="day">Day</option>
            <option value="week">Week</option>
          </select>
        </label>
        {data.calendar && (
          <span className="calendar-zone">Times in {data.zone} · revision {data.calendar.revision}</span>
        )}
      </div>
      <div className="split">
        <div className="calendar-days">
          {data.calendar && datesForView(date, view).map((day) => (
            <CalendarDay key={day} date={day} zone={data.zone} onSelect={setSelectedId}
              events={events.filter((event) => dayKey(event.start_at, data.zone) === day)} />
          ))}
        </div>
        <aside className="stack">
          <section className="card">
            <h3>Selected item</h3>
            {selected ? (
              <EventDetail key={`${selected.event_id}:${stamp.version}`} event={selected} />
            ) : (
              <p className="muted">
                {selectedId ? "Selected item is no longer on the calendar." : "Choose an appointment or block."}
              </p>
            )}
          </section>
          <section className="card">
            <h3>Block unavailable time</h3>
            <BlockForm />
          </section>
        </aside>
      </div>
    </>
  );
}

function CalendarDay({ date, zone, events, onSelect }: {
  date: string; zone: string; events: CalendarEvent[]; onSelect: (id: string) => void;
}) {
  const sorted = [...events].sort((a, b) => a.start_at.localeCompare(b.start_at));
  return (
    <section className="card day" aria-label={dayTitle(date)}>
      <h3>{dayTitle(date)}</h3>
      {sorted.length === 0 && <p className="empty">No scheduled items</p>}
      {sorted.map((event) => (
        <button key={event.event_id} type="button" onClick={() => onSelect(event.event_id)}
          className={`event ${event.status.toLowerCase().split("_")[0]}`}>
          <strong>{localTime(event.start_at, zone)}–{localTime(event.end_at, zone)}</strong>
          <span>{statusLabel(event.status)}</span>
        </button>
      ))}
    </section>
  );
}
