/** Business-timezone display helpers. Instants are ISO strings from the API. */

export type CalendarView = "day" | "week";

function parts(instant: string, zone: string, options: Intl.DateTimeFormatOptions): Record<string, string> {
  return Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: zone, ...options })
    .formatToParts(new Date(instant))
    .filter((part) => part.type !== "literal")
    .map((part) => [part.type, part.value]));
}

/** Local calendar date (YYYY-MM-DD) of an instant in the business timezone. */
export function dayKey(instant: string, zone: string): string {
  const p = parts(instant, zone, { year: "numeric", month: "2-digit", day: "2-digit" });
  return `${p.year}-${p.month}-${p.day}`;
}

export function localTime(instant: string, zone: string): string {
  return new Intl.DateTimeFormat("en-US", { timeZone: zone, hour: "numeric", minute: "2-digit" })
    .format(new Date(instant));
}

export function localStamp(instant: string, zone: string): string {
  return `${dayKey(instant, zone)} · ${localTime(instant, zone)}`;
}

/** Value for a datetime-local input showing the instant in the business timezone. */
export function localInput(instant: string, zone: string): string {
  const p = parts(instant, zone, { hourCycle: "h23", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit" });
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

/** The selected day, or the Monday-to-Sunday week containing it. */
export function datesForView(selected: string, view: "day" | "week"): string[] {
  const day = new Date(`${selected}T12:00:00Z`);
  if (Number.isNaN(day.getTime())) return [];
  if (view === "week") day.setUTCDate(day.getUTCDate() - ((day.getUTCDay() + 6) % 7));
  const count = view === "week" ? 7 : 1;
  return Array.from({ length: count }, (_, index) => {
    const copy = new Date(day);
    copy.setUTCDate(copy.getUTCDate() + index);
    return copy.toISOString().slice(0, 10);
  });
}

/** Today's calendar date in the business timezone. */
export function todayKey(zone: string, now: Date = new Date()): string {
  return dayKey(now.toISOString(), zone);
}

/** Move a calendar date by whole days. Calendar-date arithmetic, so DST never shifts it. */
export function addDays(date: string, days: number): string {
  const day = new Date(`${date}T12:00:00Z`);
  day.setUTCDate(day.getUTCDate() + days);
  return day.toISOString().slice(0, 10);
}

/** Previous (-1) or next (1) day or week for the selected view. */
export function stepDate(date: string, view: CalendarView, direction: -1 | 1): string {
  return addDays(date, direction * (view === "week" ? 7 : 1));
}

/** Header title: "October 2, 2026" for a day; "Sep – Oct 2026" or "Oct 2026" for a week. */
export function rangeTitle(date: string, view: CalendarView): string {
  const format = (value: string, options: Intl.DateTimeFormatOptions) =>
    new Date(`${value}T12:00:00Z`).toLocaleDateString("en-US", { timeZone: "UTC", ...options });
  if (view === "day") return format(date, { month: "long", day: "numeric", year: "numeric" });
  const days = datesForView(date, "week");
  const first = days[0] ?? date;
  const last = days[days.length - 1] ?? date;
  const startYear = format(first, { year: "numeric" });
  const endYear = format(last, { year: "numeric" });
  const startMonth = format(first, { month: "short" });
  const endMonth = format(last, { month: "short" });
  if (startYear !== endYear) return `${startMonth} ${startYear} – ${endMonth} ${endYear}`;
  return startMonth === endMonth ? `${startMonth} ${endYear}` : `${startMonth} – ${endMonth} ${endYear}`;
}

export function dayTitle(date: string): string {
  return new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", {
    weekday: "long", month: "short", day: "numeric", timeZone: "UTC" });
}

export function statusLabel(status: string): string {
  return status.replaceAll("_", " ");
}
