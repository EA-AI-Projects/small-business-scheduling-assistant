import type { AvailabilityPolicy, LocalWindow, PolicyBody } from "@/api/types";

import { shortTime, toPolicyBody } from "./policyEdit";

export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const;

/** Weekdays use the API numbering: 0 is Monday, 6 is Sunday. */
export interface RegularDraft {
  weekly: LocalWindow[][];
  holidays: "US_FEDERAL" | "none";
  horizon: string;
}

export const ADVANCED_FIELDS = ["slot_increment_minutes", "maximum_visit_minutes", "maximum_buffer_minutes",
  "minimum_visit_gap_minutes", "opening_buffer_minutes", "closing_buffer_minutes", "hold_minutes"] as const;
export type AdvancedField = typeof ADVANCED_FIELDS[number];
export type AdvancedDraft = Record<AdvancedField, string> & { timezone: string };

export type Errors = Record<string, string>;

export function regularDraft(policy: AvailabilityPolicy): RegularDraft {
  return {
    weekly: WEEKDAYS.map((_, day) => (policy.weekly_windows[String(day)] ?? [])
      .map((window) => ({ opens: shortTime(window.opens), closes: shortTime(window.closes) }))),
    holidays: policy.holiday_calendar === "US_FEDERAL" ? "US_FEDERAL" : "none",
    horizon: String(policy.booking_horizon_days),
  };
}

export function advancedDraft(policy: AvailabilityPolicy): AdvancedDraft {
  const draft = { timezone: policy.timezone } as AdvancedDraft;
  for (const key of ADVANCED_FIELDS) draft[key] = String(policy[key]);
  return draft;
}

function wholeNumber(value: string): number | null {
  const text = value.trim();
  const number = Number(text);
  return text !== "" && Number.isInteger(number) ? number : null;
}

export function regularErrors(draft: RegularDraft): Errors {
  const found: Errors = {};
  const horizon = wholeNumber(draft.horizon);
  if (horizon === null || horizon < 1) found.horizon = "Booking horizon must be a whole number of days, 1 or more.";
  draft.weekly.forEach((windows, day) => windows.forEach((window, index) => {
    const name = `${WEEKDAYS[day]} hours`;
    if (!window.opens || !window.closes) found[`weekly-${day}-${index}`] = `${name}: enter both an opening and a closing time.`;
    else if (window.closes <= window.opens) found[`weekly-${day}-${index}`] = `${name}: closing time must be after opening time.`;
  }));
  return found;
}

const ADVANCED_LABELS: Record<AdvancedField, { label: string; minimum: number }> = {
  slot_increment_minutes: { label: "Start-time increment", minimum: 1 },
  maximum_visit_minutes: { label: "Maximum visit length", minimum: 1 },
  maximum_buffer_minutes: { label: "Maximum buffer", minimum: 0 },
  minimum_visit_gap_minutes: { label: "Gap between visits", minimum: 0 },
  opening_buffer_minutes: { label: "Opening buffer", minimum: 0 },
  closing_buffer_minutes: { label: "Closing buffer", minimum: 0 },
  hold_minutes: { label: "Hold duration", minimum: 1 },
};
export { ADVANCED_LABELS };

export function validTimezone(value: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value });
    return value.trim() !== "";
  } catch {
    return false;
  }
}

export function advancedErrors(draft: AdvancedDraft): Errors {
  const found: Errors = {};
  for (const key of ADVANCED_FIELDS) {
    const { label, minimum } = ADVANCED_LABELS[key];
    const value = wholeNumber(draft[key]);
    if (value === null || value < minimum) {
      found[key] = `${label} must be a whole number of minutes, ${minimum} or more.`;
    }
  }
  const gap = wholeNumber(draft.minimum_visit_gap_minutes);
  const buffer = wholeNumber(draft.maximum_buffer_minutes);
  if (!found.maximum_buffer_minutes && gap !== null && buffer !== null && buffer < gap) {
    found.maximum_buffer_minutes = "Maximum buffer must be at least the gap between visits.";
  }
  if (!validTimezone(draft.timezone)) found.timezone = "Enter a valid timezone name, such as America/Los_Angeles.";
  return found;
}

/** The saved policy with the regular fields replaced by the form's values. */
export function applyRegular(policy: AvailabilityPolicy, draft: RegularDraft): PolicyBody {
  const weekly_windows: PolicyBody["weekly_windows"] = {};
  draft.weekly.forEach((windows, day) => {
    if (windows.length) weekly_windows[day] = windows.map(({ opens, closes }) => ({ opens, closes }));
  });
  return { ...toPolicyBody(policy), weekly_windows,
    holiday_calendar: draft.holidays === "US_FEDERAL" ? "US_FEDERAL" : null,
    booking_horizon_days: Number(draft.horizon) };
}

/** The saved policy with the advanced fields replaced by the form's values. */
export function applyAdvanced(policy: AvailabilityPolicy, draft: AdvancedDraft): PolicyBody {
  const body = toPolicyBody(policy);
  for (const key of ADVANCED_FIELDS) body[key] = Number(draft[key]);
  return { ...body, timezone: draft.timezone.trim() };
}
