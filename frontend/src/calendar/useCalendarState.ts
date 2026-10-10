import { useCallback, useState } from "react";

import { stepDate, todayKey, type CalendarView } from "@/lib/time";

export interface CalendarState {
  /** Selected calendar date (YYYY-MM-DD, business timezone); follows today until moved. "" before ready. */
  date: string;
  view: CalendarView;
  setView: (view: CalendarView) => void;
  /** Days covered by the Schedule view, growing by 30 with each Load more. */
  scheduleDays: number;
  loadMoreSchedule: () => void;
  goToday: () => void;
  goToDate: (date: string) => void;
  /** Open Day view on a date, even one past `maxDate` (a tapped day; the Day view explains the limit). */
  openDay: (date: string) => void;
  /** Step the range back or forward by one unit of the current view. */
  stepRange: (direction: -1 | 1) => void;
}

/**
 * Selected date, view, Schedule range and step/today/pick logic shared by every calendar.
 * Until the date is picked it is today in `zone`. Before `ready` (the zone is not yet known)
 * there is no date ("") and picking or stepping does nothing.
 */
export function useCalendarState({ ready, zone, maxDate = null }: {
  ready: boolean; zone: string;
  /**
   * Last date browsing may reach (the client app passes today plus the booking horizon). In every
   * view a step or pick to a later date lands on `maxDate`; only `openDay` (a tapped day) may go
   * past it. Null (the owner app) means no limit.
   */
  maxDate?: string | null;
}): CalendarState {
  const [pickedDate, setPickedDate] = useState<string | null>(null);
  const [view, setView] = useState<CalendarView>("day");
  const [scheduleDays, setScheduleDays] = useState(30);

  const date = pickedDate ?? (ready ? todayKey(zone) : "");
  const changeView = useCallback((next: CalendarView) => { setView(next); setScheduleDays(30); }, []);
  const goToday = useCallback(() => { setPickedDate(null); setScheduleDays(30); }, []);
  const clamp = useCallback((next: string) => (maxDate && next > maxDate ? maxDate : next), [maxDate]);
  const goToDate = useCallback((next: string) => {
    if (!ready) return;
    setPickedDate(clamp(next));
    setScheduleDays(30);
  }, [ready, clamp]);
  const openDay = useCallback((next: string) => {
    if (!ready) return;
    setPickedDate(next);
    setView("day");
    setScheduleDays(30);
  }, [ready]);
  const stepRange = useCallback((direction: -1 | 1) => {
    if (!ready) return;
    setPickedDate(clamp(stepDate(pickedDate ?? todayKey(zone), view, direction, scheduleDays)));
  }, [ready, pickedDate, zone, view, scheduleDays, clamp]);
  const loadMoreSchedule = useCallback(() => setScheduleDays((days) => days + 30), []);

  return { date, view, setView: changeView, scheduleDays, loadMoreSchedule, goToday, goToDate, openDay, stepRange };
}
