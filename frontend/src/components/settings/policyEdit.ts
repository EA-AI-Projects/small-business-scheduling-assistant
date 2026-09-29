import type { AvailabilityPolicy, LocalWindow, PolicyBody } from "@/api/types";

export type ExceptionMode = "closed" | "custom" | "normal";

function copyWindows(windows: Record<string, LocalWindow[]>): Record<string, LocalWindow[]> {
  return Object.fromEntries(Object.entries(windows).map(([key, items]) =>
    [key, items.map(({ opens, closes }) => ({ opens, closes }))]));
}

/**
 * Convert a policy as returned by GET /policy into the strict PUT body. Only fields the
 * backend's PolicyBody accepts are copied (it forbids extras); the serialized shape
 * (string weekday keys, "HH:MM:SS" times) is accepted back as-is.
 */
export function toPolicyBody(policy: AvailabilityPolicy): PolicyBody {
  return {
    timezone: policy.timezone,
    weekly_windows: copyWindows(policy.weekly_windows),
    booking_horizon_days: policy.booking_horizon_days,
    slot_increment_minutes: policy.slot_increment_minutes,
    maximum_visit_minutes: policy.maximum_visit_minutes,
    minimum_visit_gap_minutes: policy.minimum_visit_gap_minutes,
    opening_buffer_minutes: policy.opening_buffer_minutes,
    closing_buffer_minutes: policy.closing_buffer_minutes,
    hold_minutes: policy.hold_minutes,
    maximum_buffer_minutes: policy.maximum_buffer_minutes,
    date_exceptions: copyWindows(policy.date_exceptions ?? {}),
    holiday_calendar: policy.holiday_calendar as PolicyBody["holiday_calendar"],
  };
}

/** The policy with one date closed, given custom hours, or restored to normal hours. */
export function withDateException(policy: AvailabilityPolicy, date: string, mode: ExceptionMode,
  window: LocalWindow): PolicyBody {
  const body = toPolicyBody(policy);
  const exceptions = body.date_exceptions ?? {};
  if (mode === "normal") delete exceptions[date];
  else if (mode === "closed") exceptions[date] = [];
  else exceptions[date] = [{ opens: window.opens, closes: window.closes }];
  return { ...body, date_exceptions: exceptions };
}

/** "HH:MM" from an API time ("HH:MM:SS") or a time input value. */
export function shortTime(value: string): string {
  return value.slice(0, 5);
}
