import type { Appointment, CalendarEvent } from "@/api/types";

/**
 * How the visit a replacement request replaces is currently placed: still a pending request
 * with a live hold, a confirmed visit on the calendar, or neither (expired, declined, gone).
 */
export function replacementOriginal(
  request: Appointment,
  requests: Appointment[],
  events: CalendarEvent[],
  now: number,
): { original: Appointment | null; originalConfirmed: boolean } {
  const id = request.replaces_appointment_id;
  const original = requests.find((item) => item.appointment_id === id
    && item.hold_expires_at !== null && Date.parse(item.hold_expires_at) > now) ?? null;
  const originalConfirmed = events.some((event) => event.event_id === id && event.status === "CONFIRMED");
  return { original, originalConfirmed };
}
