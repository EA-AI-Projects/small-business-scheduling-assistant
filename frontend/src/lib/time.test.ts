import { describe, expect, it } from "vitest";

import { rangeTitle, stepDate, todayKey } from "./time";

// Boundary: the calendar header's date state. Not covered: rendering, menu focus, layout.
describe("calendar navigation dates", () => {
  const la = "America/Los_Angeles";
  const at = (iso: string) => todayKey(la, new Date(iso));

  it("rolls today over at business-zone midnight, including around DST changes", () => {
    // Spring forward 2026-03-08 (midnights are UTC-8 then UTC-7).
    expect(at("2026-03-08T07:59:00Z")).toBe("2026-03-07");
    expect(at("2026-03-08T08:00:00Z")).toBe("2026-03-08");
    expect(at("2026-03-09T06:59:00Z")).toBe("2026-03-08");
    expect(at("2026-03-09T07:00:00Z")).toBe("2026-03-09");
    // Fall back 2026-11-01 (midnights are UTC-7 then UTC-8).
    expect(at("2026-11-01T06:59:00Z")).toBe("2026-10-31");
    expect(at("2026-11-01T07:00:00Z")).toBe("2026-11-01");
    expect(at("2026-11-02T07:59:00Z")).toBe("2026-11-01");
    expect(at("2026-11-02T08:00:00Z")).toBe("2026-11-02");
  });

  it("uses the business zone rather than UTC", () => {
    const instant = new Date("2026-10-03T03:30:00Z");
    expect(todayKey(la, instant)).toBe("2026-10-02");
    expect(todayKey("Pacific/Auckland", instant)).toBe("2026-10-03");
  });

  it("steps one day or week across month and year ends", () => {
    expect(stepDate("2026-10-31", "day", 1)).toBe("2026-11-01");
    expect(stepDate("2026-01-01", "day", -1)).toBe("2025-12-31");
    expect(stepDate("2026-11-01", "week", -1)).toBe("2026-10-25");
  });

  it("titles a day and a week range", () => {
    expect(rangeTitle("2026-10-02", "day")).toBe("October 2, 2026");
    expect(rangeTitle("2026-10-02", "week")).toBe("Sep – Oct 2026");
    expect(rangeTitle("2026-10-14", "week")).toBe("Oct 2026");
    expect(rangeTitle("2026-12-31", "week")).toBe("Dec 2026 – Jan 2027");
  });
});
