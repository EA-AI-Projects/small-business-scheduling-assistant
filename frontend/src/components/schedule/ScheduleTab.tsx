import { useEffect, useRef, useState } from "react";

import { layoutDay } from "@/lib/calendarLayout";
import { datesForView } from "@/lib/time";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

import { SectionHeading } from "../Workspace";
import { BlockForm } from "./BlockForm";
import { CalendarGrid } from "./CalendarGrid";
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
  const days = datesForView(date, view);
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
        <div className="calendar-pane">
          {!data.calendar ? (
            <p className="empty" role="status">Calendar not loaded yet. If it does not appear, use Refresh.</p>
          ) : (
            <>
              <CalendarGrid days={days} events={events} zone={data.zone}
                selectedId={selectedId} onSelect={setSelectedId} />
              {!days.some((day) => layoutDay(events, day, data.zone).length > 0) && (
                <p className="empty" role="status">No scheduled items in this {view === "week" ? "week" : "day"}.</p>
              )}
            </>
          )}
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
