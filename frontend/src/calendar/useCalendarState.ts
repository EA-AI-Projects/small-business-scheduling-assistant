import { useCallback, useState } from "react";

import { pastLimit } from "./horizon";
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
   * Last date browsing may reach (the client app passes today plus the booking horizon). A step or
   * pick whose whole period lies after it does not go there: a step stays put, a pick lands on
   * `maxDate`. A period that merely straddles it may be shown. Null (the owner app) means no limit.
   */
  maxDate?: string | null;
}): CalendarState {
  const [pickedDate, setPickedDate] = useState<string | null>(null);
  const [view, setView] = useState<CalendarView>("day");
  const [scheduleDays, setScheduleDays] = useState(30);

  const date = pickedDate ?? (ready ? todayKey(zone) : "");
  const changeView = useCallback((next: CalendarView) => { setView(next); setScheduleDays(30); }, []);
  const goToday = useCallback(() => { setPickedDate(null); setScheduleDays(30); }, []);
  const goToDate = useCallback((next: string) => {
    if (!ready) return;
    setPickedDate(maxDate && pastLimit(next, view, maxDate) ? maxDate : next);
    setScheduleDays(30);
  }, [ready, maxDate, view]);
  const stepRange = useCallback((direction: -1 | 1) => {
    if (!ready) return;
    const next = stepDate(pickedDate ?? todayKey(zone), view, direction, scheduleDays);
    if (direction === 1 && pastLimit(next, view, maxDate)) return;
    setPickedDate(next);
  }, [ready, pickedDate, zone, view, scheduleDays, maxDate]);
  const loadMoreSchedule = useCallback(() => setScheduleDays((days) => days + 30), []);

  return { date, view, setView: changeView, scheduleDays, loadMoreSchedule, goToday, goToDate, stepRange };
}
