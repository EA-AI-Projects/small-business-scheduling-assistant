/** Business-timezone display helpers. Instants are ISO strings from the API. */

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

export function dayTitle(date: string): string {
  return new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", {
    weekday: "long", month: "short", day: "numeric", timeZone: "UTC" });
}

export function statusLabel(status: string): string {
  return status.replaceAll("_", " ");
}
