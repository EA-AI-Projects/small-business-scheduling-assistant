// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";

import { useCalendarState, type CalendarState } from "./useCalendarState";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

// Boundary: shared date/view/range state. Not covered: rendering of any calendar view.
function setup(ready: boolean, zone = "UTC") {
  const holder: { state: CalendarState | null } = { state: null };
  function Probe() {
    holder.state = useCalendarState({ ready, zone });
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
});
