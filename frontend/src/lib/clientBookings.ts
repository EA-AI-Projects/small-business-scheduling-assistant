/** Client-facing booking states. The API may add states, so unknown ones fall back safely. */

import type { CalendarItem } from "@/calendar/item";

export interface ClientBooking {
  appointment_id: string;
  status: string;
  start_at: string;
  end_at: string;
  duration_minutes: number;
  hold_expires_at: string | null;
  requested_at: string | null;
  /** The state the client saw; a cancel or move quotes it so a stale view is refused. */
  version?: number;
  /** On a pending replacement: the confirmed visit it would replace once the owner approves. */
  replaces_appointment_id?: string | null;
}

export interface BookingState {
  /** Short badge text. */
  label: string;
  /** Plain-language explanation; never implies an unconfirmed time is booked. */
  detail: string;
  tone: "pending" | "confirmed" | "ended" | "unknown";
}

const ENDED = {
  DECLINED: { label: "Declined", detail: "The business could not take this request. You may request another time." },
  EXPIRED: { label: "Expired", detail: "This request was not approved in time. You may request another time." },
  CANCELLED: { label: "Cancelled", detail: "This visit was cancelled." },
} as const;

/** Resolve the state to show; `nowMs` lets a pending hold lapse on screen before the next refresh. */
export function bookingState(booking: ClientBooking, nowMs: number): BookingState {
  if (booking.status === "CONFIRMED") {
    return { label: "Confirmed", detail: "The business has confirmed this visit.", tone: "confirmed" };
  }
  if (booking.status === "PENDING_APPROVAL") {
    const holdEnd = booking.hold_expires_at ? Date.parse(booking.hold_expires_at) : Number.NaN;
    // The server decides expiry: a fast device clock must not label a still-pending request Expired.
    if (!Number.isNaN(holdEnd) && holdEnd <= nowMs) return { label: "Checking status…", tone: "pending",
      detail: "Not confirmed. Checking with the business for the latest status; use Refresh to check again." };
    return { label: "Waiting for approval", tone: "pending",
      detail: "Not confirmed yet. This time is held for you while the owner decides; it is not an appointment until approved." };
  }
  if (booking.status in ENDED) return { ...ENDED[booking.status as keyof typeof ENDED], tone: "ended" };
  return { label: "Status unavailable", tone: "unknown",
    detail: "We could not show the status of this request. It is not confirmed; contact the business to check." };
}

export function parseBookings(data: unknown): ClientBooking[] {
  const list = (data as { bookings?: unknown } | null)?.bookings;
  if (!Array.isArray(list)) throw new Error("Unexpected bookings response");
  return list.filter((item): item is ClientBooking => typeof item === "object" && item !== null
    && typeof (item as ClientBooking).appointment_id === "string"
    && typeof (item as ClientBooking).status === "string"
    && !Number.isNaN(Date.parse((item as ClientBooking).start_at))
    && !Number.isNaN(Date.parse((item as ClientBooking).end_at)));
}

export interface ClientAvailability { starts: string[]; durationMinutes: number | null }

export function parseAvailability(data: unknown): ClientAvailability {
  const body = data as { starts_at?: unknown; duration_minutes?: unknown } | null;
  if (!Array.isArray(body?.starts_at)) throw new Error("Unexpected availability response");
  return {
    starts: body.starts_at.filter((item): item is string => typeof item === "string" && !Number.isNaN(Date.parse(item))),
    durationMinutes: typeof body.duration_minutes === "number" ? body.duration_minutes : null,
  };
}

/** Whether the server still lists a pending booking whose hold the device clock says has passed. */
export function hasStalePending(bookings: ClientBooking[], nowMs: number): boolean {
  return bookings.some((booking) => booking.status === "PENDING_APPROVAL" && booking.hold_expires_at
    && Date.parse(booking.hold_expires_at) <= nowMs);
}

/** Earliest future pending-hold expiry, so the page can re-read bookings when it passes. */
export function nextHoldExpiry(bookings: ClientBooking[], nowMs: number): number | null {
  const times = bookings.filter((booking) => booking.status === "PENDING_APPROVAL" && booking.hold_expires_at)
    .map((booking) => Date.parse(booking.hold_expires_at ?? "")).filter((time) => time > nowMs);
  return times.length ? Math.min(...times) : null;
}

/** The pending replacement waiting on this visit, if the server lists one. */
export function pendingReplacement(bookings: ClientBooking[], original: ClientBooking): ClientBooking | null {
  return bookings.find((item) => item.status === "PENDING_APPROVAL"
    && item.replaces_appointment_id === original.appointment_id) ?? null;
}

/** Whether a booking may be changed from the page: the server gave its version and it is live. */
export function hasVersion(booking: ClientBooking): booking is ClientBooking & { version: number } {
  return Number.isInteger(booking.version) && (booking.version ?? 0) > 0;
}

/** Whether a pending booking is a request to move a confirmed visit rather than a new request. */
export function isMoveRequest(booking: ClientBooking): boolean {
  return booking.status === "PENDING_APPROVAL" && Boolean(booking.replaces_appointment_id);
}

/**
 * The client's own live bookings as neutral calendar items. Only confirmed visits and pending
 * requests that have not ended are shown; the wording and colour keep a request from ever looking
 * confirmed, and a move request from looking like either a confirmed visit or a new request.
 */
export function calendarItems(bookings: ClientBooking[], nowMs: number): CalendarItem[] {
  return bookings.flatMap((booking): CalendarItem[] => {
    if (booking.status !== "CONFIRMED" && booking.status !== "PENDING_APPROVAL") return [];
    if (Date.parse(booking.end_at) <= nowMs) return [];
    const base = { event_id: booking.appointment_id, start_at: booking.start_at, end_at: booking.end_at,
      status: booking.status };
    if (isMoveRequest(booking)) return [{ ...base, label: "Move request", kind: "move" }];
    const state = bookingState(booking, nowMs);
    return [{ ...base, label: state.label, kind: state.tone === "confirmed" ? "confirmed" : "pending" }];
  });
}
