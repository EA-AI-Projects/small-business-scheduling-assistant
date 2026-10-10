// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";

import { useCalendarState, type CalendarState } from "./useCalendarState";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

// Boundary: shared date/view/range state. Not covered: rendering of any calendar view.
function setup(ready: boolean, zone = "UTC", maxDate: string | null = null) {
  const holder: { state: CalendarState | null } = { state: null };
  function Probe() {
    holder.state = useCalendarState({ ready, zone, maxDate });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  act(() => root.render(<Probe />));
  return { get: () => holder.state as CalendarState, root };
}

describe("useCalendarState", () => {
  it("has no date and ignores navigation until ready", () => {
    const { get, root } = setup(false);
    act(() => get().goToDate("2026-10-12"));
    act(() => get().stepRange(1));
    expect(get().date).toBe("");
    act(() => root.unmount());
  });

  it("picks, steps, returns to today, and resets the Schedule range", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-13T03:00:00Z"));
    try {
      const { get, root } = setup(true, "America/Los_Angeles");
      act(() => get().goToDate("2026-10-12"));
      expect(get().date).toBe("2026-10-12");
      act(() => get().stepRange(1));
      expect(get().date).toBe("2026-10-13");
      act(() => get().setView("schedule"));
      act(() => get().loadMoreSchedule());
      expect(get().scheduleDays).toBe(60);
      act(() => get().setView("week"));
      expect(get().scheduleDays).toBe(30);
      act(() => get().goToday());
      // 03:00Z on the 13th is still the 12th in Los Angeles, so the business zone is used.
      expect(get().date).toBe("2026-10-12");
      act(() => root.unmount());
    } finally {
      vi.useRealTimers();
    }
  });

  it("clamps any later step or pick to maxDate in every view, but lets openDay go past it", () => {
    const { get, root } = setup(true, "UTC", "2026-11-05");
    act(() => get().goToDate("2026-10-20"));
    for (const view of ["day", "week", "month", "year"] as const) {
      act(() => get().setView(view));
      act(() => get().goToDate("2026-10-20"));
      act(() => get().stepRange(1));
      // Month and Year steps would land past the limit, so they stop on it.
      expect(get().date <= "2026-11-05").toBe(true);
      act(() => get().goToDate("2030-01-01"));
      expect(get().date).toBe("2026-11-05");
    }
    act(() => get().setView("month"));
    act(() => get().goToDate("2026-10-20"));
    act(() => get().stepRange(1));
    expect(get().date).toBe("2026-11-05");
    act(() => get().openDay("2026-11-20"));
    expect(get().date).toBe("2026-11-20");
    expect(get().view).toBe("day");
    act(() => root.unmount());
  });
});
