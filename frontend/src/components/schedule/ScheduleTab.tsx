import { useCallback, useRef, useState } from "react";

import { layoutDay } from "@/lib/calendarLayout";
import { datesForView } from "@/lib/time";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

import { SectionHeading } from "../Workspace";
import { BlockForm } from "./BlockForm";
import { CalendarGrid } from "./CalendarGrid";
import { EventCard } from "./EventCard";

export function ScheduleTab() {
  const { data, stamp, refresh, notify, date, view } = useOwner();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [handledStamp, setHandledStamp] = useState(stamp.version);
  const [handledRange, setHandledRange] = useState(`${date}|${view}`);
  // The element that opened the card; it anchors the card and gets focus back on close.
  const origin = useRef<HTMLElement | null>(null);

  // A normal refresh closes the card; a conflict refresh keeps it open if the item is still present.
  if (stamp.version !== handledStamp) {
    setHandledStamp(stamp.version);
    if (!stamp.preserveSelection) setSelectedId(null);
  }
  // Moving to another day or week closes the card: its item is no longer on screen.
  if (`${date}|${view}` !== handledRange) {
    setHandledRange(`${date}|${view}`);
    setSelectedId(null);
  }

  const events = data.calendar?.events ?? [];
  const selected = selectedId ? events.find((event) => event.event_id === selectedId) : undefined;
  // The item vanished (for example after a conflict refresh): close the card.
  if (selectedId && data.calendar && !selected) setSelectedId(null);

  const findItem = useCallback((id: string): HTMLElement | null => {
    if (origin.current?.isConnected) return origin.current;
    return document.querySelector<HTMLElement>(`[data-event-id="${CSS.escape(id)}"]`);
  }, []);
  const getAnchor = useCallback(
    () => (selectedId ? findItem(selectedId) : null), [findItem, selectedId]);
  const close = useCallback(() => setSelectedId(null), []);
  const select = (id: string, element: HTMLElement) => {
    origin.current = element;
    setSelectedId((current) => (current === id ? null : id));
  };

  // After the card closes, put focus back on the item, or on the calendar if the item is gone.
  // Where focus goes when the card closes: the item, or the calendar if the item is gone.
  const returnFocus = useCallback(
    () => (selectedId ? findItem(selectedId) : null) ?? document.querySelector<HTMLElement>("[data-calendar-scroll]"),
    [findItem, selectedId]);
  // A press on another item switches the card to it; a press on the open item closes it.
  const pressItem = (element: HTMLElement) => {
    const id = element.dataset.eventId;
    if (id) select(id, element); else close();
  };

  const days = datesForView(date, view);

  return (
    <>
      <SectionHeading eyebrow="CALENDAR" title="Schedule">
        <button type="button" onClick={() => { notify(""); refresh().catch((error: unknown) => notify(errorMessage(error), true)); }}>
          Refresh
        </button>
      </SectionHeading>
      {data.calendar && (
        <p className="calendar-zone">Times in {data.zone} · revision {data.calendar.revision}</p>
      )}
      <div className="calendar-pane">
        {!data.calendar ? (
          <p className="empty" role="status">Calendar not loaded yet. If it does not appear, use Refresh.</p>
        ) : (
          <>
            <CalendarGrid days={days} events={events} zone={data.zone}
              selectedId={selectedId} onSelect={select} />
            {!days.some((day) => layoutDay(events, day, data.zone).length > 0) && (
              <p className="empty" role="status">No scheduled items in this {view === "week" ? "week" : "day"}.</p>
            )}
          </>
        )}
      </div>
      {selected && <EventCard event={selected} getAnchor={getAnchor} onClose={close} onAnchorPress={pressItem}
        returnFocus={returnFocus} />}
      {/* Temporary home for the block form until the empty-slot card replaces it (#153). */}
      <section className="card block-card">
        <h3>Block unavailable time</h3>
        <BlockForm />
      </section>
    </>
  );
}
