import { addDays } from "./time";

export interface GridDay {
  /** Calendar date, YYYY-MM-DD. */
  date: string;
  /** Day of month, 1-31. */
  day: number;
  /** False for the leading and trailing days of the neighbouring months. */
  inMonth: boolean;
}

/** First day of the month containing a date. */
export function monthStart(date: string): string {
  return `${date.slice(0, 7)}-01`;
}

/** Move a date by whole months, keeping the day of month or clamping to the month's last day. */
export function addMonths(date: string, months: number): string {
  const index = Number(date.slice(0, 4)) * 12 + (Number(date.slice(5, 7)) - 1) + months;
  const year = Math.floor(index / 12);
  const month = index - year * 12;
  const last = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  const day = Math.min(Number(date.slice(8, 10)), last);
  return `${String(year).padStart(4, "0")}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/**
 * The 6 weeks (42 days) a mini calendar shows for the month containing `date`, Monday first.
 * Pure calendar-date arithmetic, so time zones and DST cannot skip or repeat a day.
 */
export function monthGrid(date: string): GridDay[][] {
  const first = monthStart(date);
  const weekday = (new Date(`${first}T12:00:00Z`).getUTCDay() + 6) % 7; // Monday = 0
  const month = first.slice(0, 7);
  return Array.from({ length: 6 }, (_, week) => Array.from({ length: 7 }, (_, index) => {
    const key = addDays(first, week * 7 + index - weekday);
    return { date: key, day: Number(key.slice(8, 10)), inMonth: key.startsWith(month) };
  }));
}
