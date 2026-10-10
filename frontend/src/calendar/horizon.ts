import { monthStart } from "@/lib/monthGrid";
import { datesForView, type CalendarView } from "@/lib/time";

/** First date the period of `view` around `date` covers. */
export function periodStart(date: string, view: CalendarView): string {
  if (view === "week") return datesForView(date, "week")[0] ?? date;
  if (view === "month") return monthStart(date);
  if (view === "year") return `${date.slice(0, 4)}-01-01`;
  return date;
}

/** True when the whole period lies past `maxDate` (no limit when `maxDate` is null or undefined). */
export function pastLimit(date: string, view: CalendarView, maxDate: string | null | undefined): boolean {
  return Boolean(maxDate) && periodStart(date, view) > (maxDate as string);
}
