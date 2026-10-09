import { useCallback, useRef, useState } from "react";

import { layoutDay } from "@/lib/calendarLayout";
import { nextSlotOn, slotAtFraction, type SlotRange } from "@/lib/slots";
import { datesForView } from "@/lib/time";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

import type { CardAnchor } from "../PopoverCard";
import { SectionHeading } from "../Workspace";
import { CalendarGrid } from "./CalendarGrid";
import { EventCard } from "./EventCard";
import { MonthGrid } from "./MonthGrid";
import { ScheduleList } from "./ScheduleList";
import { SlotCard } from "./SlotCard";
import { YearGrid } from "./YearGrid";

/** The open "block time" card. `key` changes with each opening so the form restarts from its prefill. */
interface Slot { range: SlotRange; key: number; anchor: CardAnchor; opener: HTMLElement | null }

export function ScheduleTab() {
  const { data, stamp, refresh, notify, date, view, setView, goToDate, scheduleDays, loadMoreSchedule } = useOwner();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [handledStamp, setHandledStamp] = useState(stamp.version);
  const [handledRange, setHandledRange] = useState(`${date}|${view}`);
  // The element that opened the card; it anchors the card and gets focus back on close.
  const origin = useRef<HTMLElement | null>(null);
  // At most one card is open: an item card or the block-time card.
  const [slot, setSlot] = useState<Slot | null>(null);
  const openings = useRef(0);

  // A normal refresh closes the card; a conflict refresh keeps it open if the item is still present.
  if (stamp.version !== handledStamp) {
    setHandledStamp(stamp.version);
    if (!stamp.preserveSelection) { setSelectedId(null); setSlot(null); }
  }
  // Moving to another day or week closes the card: its item is no longer on screen.
  if (`${date}|${view}` !== handledRange) {
    setHandledRange(`${date}|${view}`);
    setSelectedId(null);
    setSlot(null);
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
  const close = useCallback(() => { setSelectedId(null); setSlot(null); }, []);
  const select = (id: string, element: HTMLElement) => {
    origin.current = element;
    setSlot(null);
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

  const days = view === "day" || view === "week" ? datesForView(date, view) : [];

  // Open the block-time card on a slot, replacing any open card.
  const openSlot = (range: SlotRange, anchor: CardAnchor, opener: HTMLElement | null) => {
    openings.current += 1;
    setSelectedId(null);
    setSlot({ range, key: openings.current, anchor, opener });
  };
  // A press on an empty part of a day column: the slot under it, snapped to 30 minutes.
  const pressSlot = (column: HTMLElement, _x: number, y: number) => {
    const day = column.dataset.day;
    const box = column.getBoundingClientRect();
    if (!day || box.height <= 0) return;
    const range = slotAtFraction(day, (y - box.top) / box.height, data.zone);
    const minute = Number(range.start.slice(11, 13)) * 60 + Number(range.start.slice(14, 16));
    // The anchor follows the slot as the grid scrolls: its rectangle is read from the column each time.
    const anchor: CardAnchor = {
      getBoundingClientRect: () => {
        const rect = column.getBoundingClientRect();
        const top = rect.top + (minute / 1440) * rect.height;
        return { left: rect.left, right: rect.right, top, bottom: top + rect.height / 48 };
      },
    };
    openSlot(range, anchor, null);
  };
  const blockButton = useRef<HTMLButtonElement>(null);
  const openFromControl = () => {
    const button = blockButton.current;
    if (!button || !date) return;
    openSlot(nextSlotOn(date, new Date(), data.zone), button, button);
  };
  const slotAnchor = slot?.anchor;
  const getSlotAnchor = useCallback(() => slotAnchor ?? null, [slotAnchor]);
  const slotOpener = slot?.opener;
  const slotReturnFocus = useCallback(
    () => (slotOpener?.isConnected ? slotOpener : null) ?? document.querySelector<HTMLElement>("[data-calendar-scroll]"),
    [slotOpener]);

  return (
    <>
      <SectionHeading eyebrow="CALENDAR" title="Schedule">
        <div className="heading-actions">
          <button type="button" ref={blockButton} aria-haspopup="dialog" disabled={!data.calendar}
            onClick={openFromControl}>
            Block time
          </button>
          <button type="button" onClick={() => { notify(""); refresh().catch((error: unknown) => notify(errorMessage(error), true)); }}>
            Refresh
          </button>
        </div>
      </SectionHeading>
      {data.calendar && (
        <p className="calendar-zone">Times in {data.zone} · revision {data.calendar.revision}</p>
      )}
      <div className="calendar-pane">
        {!data.calendar ? (
          <p className="empty" role="status">Calendar not loaded yet. If it does not appear, use Refresh.</p>
        ) : (
          <>
            {view === "month" ? (
              <MonthGrid date={date} events={events} zone={data.zone} selectedId={selectedId}
                onSelect={select} onDay={(day) => { goToDate(day); setView("day"); }} />
            ) : view === "schedule" ? (
              <ScheduleList date={date} days={scheduleDays} events={events} zone={data.zone}
                selectedId={selectedId} onSelect={select} onLoadMore={loadMoreSchedule} />
            ) : view === "year" ? (
              <YearGrid date={date} events={events} zone={data.zone} selectedId={selectedId}
                onSelect={select} onDismiss={close} onNavigate={goToDate}
                onDay={(day) => { goToDate(day); setView("day"); }} />
            ) : (
              <CalendarGrid days={days} events={events} policy={data.policy?.record.policy} zone={data.zone}
                selectedId={selectedId} onSelect={select} onSlotPress={pressSlot} />
            )}
            {(view === "day" || view === "week") && !days.some((day) => layoutDay(events, day, data.zone).length > 0) && (
              <p className="empty" role="status">No scheduled items in this {view === "week" ? "week" : "day"}.</p>
            )}
          </>
        )}
      </div>
      {selected && <EventCard event={selected} getAnchor={getAnchor} onClose={close} onAnchorPress={pressItem}
        onSlotPress={pressSlot} returnFocus={returnFocus} />}
      {slot && <SlotCard range={slot.range} openKey={slot.key} getAnchor={getSlotAnchor} onClose={close}
        onAnchorPress={pressItem} onSlotPress={pressSlot} returnFocus={slotReturnFocus} />}
    </>
  );
}
