/** Client-facing booking states. The API may add states, so unknown ones fall back safely. */

export interface ClientBooking {
  appointment_id: string;
  status: string;
  start_at: string;
  end_at: string;
  duration_minutes: number;
  hold_expires_at: string | null;
  requested_at: string | null;
}

export interface BookingState {
  /** Short badge text. */
  label: string;
  /** Plain-language explanation; never implies an unconfirmed time is booked. */
  detail: string;
  tone: "pending" | "confirmed" | "ended" | "unknown";
}

const ENDED = {
  DECLINED: { label: "Declined", detail: "The business could not take this request. The time was released; you may request another time." },
  EXPIRED: { label: "Expired", detail: "This request was not approved in time and the time was released. You may request another time." },
  CANCELLED: { label: "Cancelled", detail: "This visit was cancelled." },
} as const;

/** Resolve the state to show; `nowMs` lets a pending hold lapse on screen before the next refresh. */
export function bookingState(booking: ClientBooking, nowMs: number): BookingState {
  if (booking.status === "CONFIRMED") {
    return { label: "Confirmed", detail: "The business has confirmed this visit.", tone: "confirmed" };
  }
  if (booking.status === "PENDING_APPROVAL") {
    const holdEnd = booking.hold_expires_at ? Date.parse(booking.hold_expires_at) : Number.NaN;
    if (!Number.isNaN(holdEnd) && holdEnd <= nowMs) return { ...ENDED.EXPIRED, tone: "ended" };
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

export function parseStarts(data: unknown): string[] {
  const list = (data as { starts_at?: unknown } | null)?.starts_at;
  if (!Array.isArray(list)) throw new Error("Unexpected availability response");
  return list.filter((item): item is string => typeof item === "string" && !Number.isNaN(Date.parse(item)));
}
