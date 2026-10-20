import { useCallback, useRef, useState } from "react";

import type { CalendarItem } from "@/calendar/item";
import type { CalendarState } from "@/calendar/useCalendarState";
import { bookingState, hasVersion, isMoveRequest, pendingReplacement, type ClientBooking } from "@/lib/clientBookings";
import { addMonths, monthStart } from "@/lib/monthGrid";
import { scheduleEvents } from "@/lib/scheduleEvents";
import { addDays, datesForView, dayKey, localStamp, localTime, todayKey, type CalendarView } from "@/lib/time";

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
 * Year views. Day and Week also place client-scoped available starts in their time grids.
 * Tapping a day in Month, Year, or a Week heading opens Day.
 */
export function ClientCalendarViews({ items, bookings, calendar, zone, nowMs, loading, error, maxDate = null,
  availableStarts = [], recommendedStarts = [], selectedStart = null, onAvailableSelect, onEventSelect, confirming, changing = false,
  changeError, onCancel, onConfirmCancel, onDismissCancel, onMove }: {
  items: CalendarItem[]; bookings: ClientBooking[]; calendar: CalendarState; zone: string; nowMs: number;
  loading: boolean; error: string | null;
  /** Last browsable date (booking horizon); Schedule offers no Load more past it. */
  maxDate?: string | null;
  availableStarts?: string[]; selectedStart?: string | null;
  /** The offered starts the business recommends; all of availableStarts stay selectable. */
  recommendedStarts?: string[];
  onAvailableSelect?: (start: string, element: HTMLElement) => void;
  onEventSelect?: () => void;
  confirming?: ClientBooking | null; changing?: boolean; changeError?: string | null;
  onCancel?: (booking: ClientBooking) => void;
  onConfirmCancel?: () => void; onDismissCancel?: () => void;
  onMove?: (booking: ClientBooking) => void;
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
  const close = useCallback(() => { setSelectedId(null); onDismissCancel?.(); }, [onDismissCancel]);
  const select = (id: string, element: HTMLElement) => {
    onEventSelect?.();
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
  const replacement = selectedBooking?.status === "CONFIRMED"
    ? pendingReplacement(bookings, selectedBooking) : null;
  const selectedState = selectedBooking ? bookingState(selectedBooking, nowMs) : null;
  const canChange = selectedBooking && hasVersion(selectedBooking)
    && (selectedState?.tone === "confirmed" || selectedState?.label === "Waiting for approval")
    && Date.parse(selectedBooking.end_at) > nowMs;
  // The booking vanished on a refresh: close its card.
  if (selectedId && !loading && !selectedItem) setSelectedId(null);

  const goDay = calendar.openDay;
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
        {recommendedStarts.length > 0 && <li><i className="recommended" aria-hidden="true" />★ Recommended time (every open time can still be chosen)</li>}
      </ul>
      {error ? <p className="notice error" role="alert">{error}</p>
        : loading ? <p role="status">Loading your appointments…</p> : null}
      {first < today && <p className="meta" role="note">Past visits are not shown.</p>}
      <div className="calendar-pane">
        {view === "month" ? (
          <MonthGrid date={date} events={items} zone={zone} selectedId={selectedId} onSelect={select} onDay={goDay} />
        ) : view === "schedule" ? (
          <ScheduleList groups={groups} zone={zone} selectedId={selectedId} onSelect={select}
            onLoadMore={maxDate && last >= maxDate ? undefined : calendar.loadMoreSchedule} itemTitle={itemTitle} />
        ) : view === "year" ? (
          <YearGrid date={date} events={items} zone={zone} selectedId={selectedId} onSelect={select}
            onDismiss={close} onNavigate={calendar.goToDate} onDay={goDay} />
        ) : (
          <CalendarGrid days={days} events={items} zone={zone} selectedId={selectedId} onSelect={select}
            onSlotPress={noSlot} onDay={goDay} availableStarts={availableStarts} recommendedStarts={recommendedStarts} selectedStart={selectedStart}
            onAvailableSelect={(start, element) => { setSelectedId(null); onAvailableSelect?.(start, element); }} />
        )}
      </div>
      {!loading && !error && !anyInRange && (view === "day" || view === "week") && (
        <p className="empty" role="status">You have no appointments or requests in this {unit}.</p>
      )}
      {selectedItem && selectedBooking && (
        <EventCard item={selectedItem} zone={zone} title={itemTitle(selectedItem)} getAnchor={getAnchor}
          onClose={close} onAnchorPress={pressItem} onSlotPress={noSlot} returnFocus={returnFocus}
          refocusKey={`${selectedBooking.appointment_id}|${selectedBooking.version ?? 0}|${confirming?.appointment_id === selectedBooking.appointment_id ? "confirm" : "detail"}`}>
          <p className="meta">Times are in the business time zone ({zone}).</p>
          <p className="meta">{detail}</p>
          {replacement && <p className="meta">A move request for {localStamp(replacement.start_at, zone)} is waiting for the owner.
            This appointment stays confirmed until then. Withdraw the move request in Appointments before changing it.</p>}
          {canChange && !replacement && <div className="actions">
            {selectedBooking.status === "CONFIRMED" && <button type="button" onClick={() => {
              onMove?.(selectedBooking); close();
            }}>Move to another time</button>}
            <button type="button" onClick={() => onCancel?.(selectedBooking)}>
              {selectedBooking.status === "CONFIRMED" ? "Cancel appointment"
                : isMoveRequest(selectedBooking) ? "Withdraw move request" : "Cancel request"}</button>
          </div>}
          {confirming?.appointment_id === selectedBooking.appointment_id && <div role="group" aria-label="Confirm cancellation" className="notice">
            <p>{selectedBooking.status === "CONFIRMED" ? "Cancel this confirmed appointment?"
              : isMoveRequest(selectedBooking) ? "Withdraw this move request? Your original appointment stays confirmed."
                : "Cancel this request?"}{" "}
              <strong>{localStamp(selectedBooking.start_at, zone)}</strong> to {localTime(selectedBooking.end_at, zone)} ({zone}).
              {selectedBooking.status === "CONFIRMED" ? " The time is released and the owner is told." : ""}</p>
            <button type="button" disabled={changing} onClick={onConfirmCancel}>
              {changing ? "Cancelling…" : isMoveRequest(selectedBooking) ? "Yes, withdraw this request"
                : `Yes, cancel this ${selectedBooking.status === "CONFIRMED" ? "appointment" : "request"}`}</button>
            <button type="button" disabled={changing} onClick={onDismissCancel}>
              {isMoveRequest(selectedBooking) ? "No, keep the request" : "No, keep it"}</button>
          </div>}
          {changeError && <p role="alert" className="notice error">{changeError}</p>}
        </EventCard>
      )}
    </div>
  );
}
