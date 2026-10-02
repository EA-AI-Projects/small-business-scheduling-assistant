import { describe, expect, it } from "vitest";

import { datesForView, rangeTitle, stepDate, todayKey } from "./time";

// Boundary: the calendar header's date state. Not covered: rendering and focus handling.
describe("calendar navigation dates", () => {
  it("computes today in the business timezone, not the browser's", () => {
    const instant = new Date("2026-10-03T03:30:00Z");
    expect(todayKey("America/Los_Angeles", instant)).toBe("2026-10-02");
    expect(todayKey("Pacific/Auckland", instant)).toBe("2026-10-03");
  });

  it("steps by one day or one week across month, year and DST changes", () => {
    expect(stepDate("2026-10-31", "day", 1)).toBe("2026-11-01");
    expect(stepDate("2026-01-01", "day", -1)).toBe("2025-12-31");
    expect(stepDate("2026-11-01", "week", -1)).toBe("2026-10-25");
    // US DST ends 2026-11-01 and starts 2026-03-08; a day is still one calendar date.
    expect(stepDate("2026-11-01", "day", 1)).toBe("2026-11-02");
    expect(stepDate("2026-03-07", "week", 1)).toBe("2026-03-14");
  });

  it("titles a day and a week range", () => {
    expect(rangeTitle("2026-10-02", "day")).toBe("October 2, 2026");
    expect(rangeTitle("2026-10-02", "week")).toBe("Sep – Oct 2026");
    expect(rangeTitle("2026-10-14", "week")).toBe("Oct 2026");
    expect(rangeTitle("2026-12-31", "week")).toBe("Dec 2026 – Jan 2027");
  });

  it("keeps the week range consistent with its title across DST", () => {
    const days = datesForView("2026-11-01", "week");
    expect(days).toHaveLength(7);
    expect(days[0]).toBe("2026-10-26");
    expect(days[6]).toBe("2026-11-01");
    expect(rangeTitle("2026-11-01", "week")).toBe("Oct – Nov 2026");
  });
});
