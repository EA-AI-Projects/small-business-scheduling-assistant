import { useEffect, useRef, useState } from "react";

import type { CalendarEvent } from "@/api/types";
import { datesForView, dayKey, dayTitle, localTime, statusLabel } from "@/lib/time";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

import { SectionHeading } from "../Workspace";
import { BlockForm } from "./BlockForm";
import { EventDetail } from "./EventDetail";

export function ScheduleTab() {
  const { data, stamp, refresh, notify, date, view } = useOwner();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [handledStamp, setHandledStamp] = useState(stamp.version);

  // A normal refresh clears the selection; a conflict refresh keeps it if still present.
  if (stamp.version !== handledStamp) {
    setHandledStamp(stamp.version);
    if (!stamp.preserveSelection) setSelectedId(null);
  }

  // On narrow screens the detail card sits below the calendar; bring it into view on selection.
  const detailRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (selectedId) detailRef.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [selectedId]);

  const events = data.calendar?.events ?? [];
  const selected = selectedId ? events.find((event) => event.event_id === selectedId) : undefined;

  return (
    <>
      <SectionHeading eyebrow="CALENDAR" title="Schedule">
        <button type="button" onClick={() => refresh().catch((error: unknown) => notify(errorMessage(error), true))}>
          Refresh
        </button>
      </SectionHeading>
      {data.calendar && (
        <p className="calendar-zone">Times in {data.zone} · revision {data.calendar.revision}</p>
      )}
      <div className="split">
        <div className="calendar-days">
          {data.calendar && datesForView(date, view).map((day) => (
            <CalendarDay key={day} date={day} zone={data.zone} onSelect={setSelectedId}
              events={events.filter((event) => dayKey(event.start_at, data.zone) === day)} />
          ))}
        </div>
        <aside className="stack">
          <section className="card" ref={detailRef} aria-live="polite">
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
