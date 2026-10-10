import { useCallback, useRef, useState } from "react";

import type { CalendarItem } from "@/calendar/item";
import type { CalendarState } from "@/calendar/useCalendarState";
import { bookingState, isMoveRequest, type ClientBooking } from "@/lib/clientBookings";
import { addMonths, monthStart } from "@/lib/monthGrid";
import { scheduleEvents } from "@/lib/scheduleEvents";
import { addDays, datesForView, dayKey, todayKey, type CalendarView } from "@/lib/time";

import { CalendarGrid } from "../schedule/CalendarGrid";
import { EventCard } from "../schedule/EventCard";
import { MonthGrid } from "../schedule/MonthGrid";
import { ScheduleList } from "../schedule/ScheduleList";
import { YearGrid } from "../schedule/YearGrid";

/** First and last calendar date the current view covers. */
export function viewRange(date: string, view: CalendarView, scheduleDays: number): [string, string] {
  if (view === "day") return [date, date];
  if (view === "week") { const days = datesForView(date, "week"); return [days[0] ?? date, days[6] ?? date]; }
  if (view === "month") return [monthStart(date), addDays(addMonths(monthStart(date), 1), -1)];
  if (view === "year") return [`${date.slice(0, 4)}-01-01`, `${date.slice(0, 4)}-12-31`];
  return [date, addDays(date, scheduleDays - 1)];
}

const noSlot = () => undefined;

/**
 * The client's own appointments and pending requests in the shared Day, Week, Month, Schedule and
 * Year views. These views show no open times; the Day view's request flow lives beside them.
 * Tapping a day in Month, Year, or a Week heading opens Day.
 */
export function ClientCalendarViews({ items, bookings, calendar, zone, nowMs, loading, error }: {
  items: CalendarItem[]; bookings: ClientBooking[]; calendar: CalendarState; zone: string; nowMs: number;
  loading: boolean; error: string | null;
}) {
  const { date, view, scheduleDays } = calendar;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [handledRange, setHandledRange] = useState(`${date}|${view}`);
  const origin = useRef<HTMLElement | null>(null);
  // Moving to another date or view closes the card: its item is no longer on screen.
  if (`${date}|${view}` !== handledRange) { setHandledRange(`${date}|${view}`); setSelectedId(null); }

  const findItem = useCallback((id: string): HTMLElement | null => {
    if (origin.current?.isConnected) return origin.current;
    return document.querySelector<HTMLElement>(`[data-event-id="${CSS.escape(id)}"]`);
  }, []);
  const close = useCallback(() => setSelectedId(null), []);
  const select = (id: string, element: HTMLElement) => {
    origin.current = element;
    setSelectedId((current) => (current === id ? null : id));
  };
  const pressItem = (element: HTMLElement) => {
    const id = element.dataset.eventId;
    if (id) select(id, element); else close();
  };
  const getAnchor = useCallback(() => (selectedId ? findItem(selectedId) : null), [findItem, selectedId]);
  const returnFocus = useCallback(() => (selectedId ? findItem(selectedId) : null)
    ?? document.querySelector<HTMLElement>("[data-calendar-scroll]"), [findItem, selectedId]);

  const selectedItem = selectedId ? items.find((item) => item.event_id === selectedId) : undefined;
  const selectedBooking = selectedId ? bookings.find((item) => item.appointment_id === selectedId) : undefined;
  // The booking vanished on a refresh: close its card.
  if (selectedId && !loading && !selectedItem) setSelectedId(null);

  const goDay = (day: string) => { calendar.goToDate(day); calendar.setView("day"); };
  const days = view === "day" || view === "week" ? datesForView(date, view) : [];
  const [first, last] = viewRange(date, view, scheduleDays);
  const today = todayKey(zone);
  const anyInRange = items.some((item) => dayKey(item.start_at, zone) <= last && dayKey(item.end_at, zone) >= first);
  const groups = scheduleEvents(items, date, scheduleDays, zone);
  const unit = view === "schedule" ? "range" : view;

  const itemTitle = (item: CalendarItem) => item.kind === "move" ? "Your move request"
    : item.kind === "confirmed" ? "Your appointment" : "Your request";
  const detail = selectedBooking ? isMoveRequest(selectedBooking)
    ? "Not confirmed. This is a request to move one of your appointments to this time. Your original appointment stays confirmed until the owner approves it."
    : bookingState(selectedBooking, nowMs).detail : "";

  return (
    <div className="client-calendar-views">
      <ul className="client-legend" aria-label="What the colours mean">
        <li><i className="confirmed" aria-hidden="true" />Confirmed</li>
        <li><i className="pending" aria-hidden="true" />Waiting for approval (not confirmed)</li>
        <li><i className="move" aria-hidden="true" />Move request (not confirmed)</li>
      </ul>
      {error ? <p className="notice error" role="alert">{error}</p>
        : loading ? <p role="status">Loading your appointments…</p> : null}
      {first < today && <p className="meta" role="note">Past visits are not shown.</p>}
      <div className="calendar-pane">
        {view === "month" ? (
          <MonthGrid date={date} events={items} zone={zone} selectedId={selectedId} onSelect={select} onDay={goDay} />
        ) : view === "schedule" ? (
          <ScheduleList groups={groups} zone={zone} selectedId={selectedId} onSelect={select}
            onLoadMore={calendar.loadMoreSchedule} itemTitle={itemTitle} />
        ) : view === "year" ? (
          <YearGrid date={date} events={items} zone={zone} selectedId={selectedId} onSelect={select}
            onDismiss={close} onNavigate={calendar.goToDate} onDay={goDay} />
        ) : (
          <CalendarGrid days={days} events={items} zone={zone} selectedId={selectedId} onSelect={select}
            onSlotPress={noSlot} onDay={goDay} />
        )}
      </div>
      {!loading && !error && !anyInRange && (view === "day" || view === "week") && (
        <p className="empty" role="status">You have no appointments or requests in this {unit}.</p>
      )}
      {selectedItem && selectedBooking && (
        <EventCard item={selectedItem} zone={zone} title={itemTitle(selectedItem)} getAnchor={getAnchor}
          onClose={close} onAnchorPress={pressItem} onSlotPress={noSlot} returnFocus={returnFocus}
          refocusKey={`${selectedBooking.appointment_id}|${selectedBooking.version ?? 0}`}>
          <p className="meta">Times are in the business time zone ({zone}).</p>
          <p className="meta">{detail}</p>
          <p className="meta">To move or cancel, use Appointments in the menu.</p>
        </EventCard>
      )}
    </div>
  );
}
