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
  /** Step the range back or forward by one unit of the current view. */
  stepRange: (direction: -1 | 1) => void;
}

/**
 * Selected date, view, Schedule range and step/today/pick logic shared by every calendar.
 * Until the date is picked it is today in `zone`. Before `ready` (the zone is not yet known)
 * there is no date ("") and picking or stepping does nothing.
 */
export function useCalendarState({ ready, zone }: { ready: boolean; zone: string }): CalendarState {
  const [pickedDate, setPickedDate] = useState<string | null>(null);
  const [view, setView] = useState<CalendarView>("day");
  const [scheduleDays, setScheduleDays] = useState(30);

  const date = pickedDate ?? (ready ? todayKey(zone) : "");
  const changeView = useCallback((next: CalendarView) => { setView(next); setScheduleDays(30); }, []);
  const goToday = useCallback(() => { setPickedDate(null); setScheduleDays(30); }, []);
  const goToDate = useCallback((next: string) => { if (ready) { setPickedDate(next); setScheduleDays(30); } }, [ready]);
  const stepRange = useCallback((direction: -1 | 1) => {
    if (!ready) return;
    setPickedDate(stepDate(pickedDate ?? todayKey(zone), view, direction, scheduleDays));
  }, [ready, pickedDate, zone, view, scheduleDays]);
  const loadMoreSchedule = useCallback(() => setScheduleDays((days) => days + 30), []);

  return { date, view, setView: changeView, scheduleDays, loadMoreSchedule, goToday, goToDate, stepRange };
}
