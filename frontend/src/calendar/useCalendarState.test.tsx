// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";

import { useCalendarState, type CalendarState } from "./useCalendarState";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Boundary: shared date/view/range state. Not covered: rendering of any calendar view.
function setup(ready: boolean) {
  const holder: { state: CalendarState | null } = { state: null };
  function Probe() {
    holder.state = useCalendarState({ ready, zone: "UTC" });
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
    const { get, root } = setup(true);
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
    expect(get().date).toBe(new Date().toISOString().slice(0, 10));
    act(() => root.unmount());
  });
});
