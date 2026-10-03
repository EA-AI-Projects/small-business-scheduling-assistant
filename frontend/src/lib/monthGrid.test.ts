import { describe, expect, it } from "vitest";

import { addMonths, monthGrid } from "./monthGrid";
import { addDays, todayKey } from "./time";

const flat = (date: string) => monthGrid(date).flat();

describe("monthGrid", () => {
  it("is always 6 weeks of 7 days", () => {
    for (const month of ["2026-02", "2026-03", "2026-08", "2026-10", "2027-02"]) {
      const grid = monthGrid(`${month}-15`);
      expect(grid).toHaveLength(6);
      grid.forEach((week) => expect(week).toHaveLength(7));
    }
  });

  it("starts on the 1st when the month begins on a Monday (June 2026)", () => {
    const days = flat("2026-06-10");
    expect(days[0]).toEqual({ date: "2026-06-01", day: 1, inMonth: true });
    expect(days.filter((d) => d.inMonth)).toHaveLength(30);
    expect(days[30]?.date).toBe("2026-07-01");
    expect(days[30]?.inMonth).toBe(false);
  });

  it("shows six leading days when the month begins on a Sunday (Feb 2026)", () => {
    const days = flat("2026-02-01");
    expect(days.slice(0, 6).map((d) => d.date)).toEqual(
      ["2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30", "2026-01-31"]);
    expect(days.slice(0, 6).every((d) => !d.inMonth)).toBe(true);
    expect(days[6]).toEqual({ date: "2026-02-01", day: 1, inMonth: true });
  });

  it("has 28 days in February 2027 and 29 in February 2028", () => {
    expect(flat("2027-02-10").filter((d) => d.inMonth)).toHaveLength(28);
    expect(flat("2028-02-10").filter((d) => d.inMonth)).toHaveLength(29);
    expect(flat("2028-02-10").some((d) => d.date === "2028-02-29" && d.inMonth)).toBe(true);
    expect(flat("2027-02-10").some((d) => d.date === "2027-02-29")).toBe(false);
  });

  it("crosses the year boundary in both directions", () => {
    const dec = flat("2026-12-31");
    expect(dec.filter((d) => d.inMonth)).toHaveLength(31);
    expect(dec[dec.length - 1]?.date).toBe("2027-01-10");
    expect(dec.some((d) => d.date === "2027-01-01" && !d.inMonth)).toBe(true);
    const jan = flat("2027-01-01");
    expect(jan[0]?.date).toBe("2026-12-28");
    expect(jan[0]?.inMonth).toBe(false);
    expect(jan[4]).toEqual({ date: "2027-01-01", day: 1, inMonth: true });
  });

  it.each(["2026-03", "2026-11"])("never skips or repeats a day in DST month %s", (month) => {
    const days = flat(`${month}-15`).map((d) => d.date);
    days.forEach((date, index) => {
      if (index > 0) expect(date).toBe(addDays(days[index - 1] ?? "", 1));
    });
    expect(new Set(days).size).toBe(42);
    expect(days.filter((d) => d.startsWith(month))).toHaveLength(month === "2026-03" ? 31 : 30);
  });

  it("agrees with the business-timezone day key across the DST changes", () => {
    const zone = "America/Los_Angeles";
    // 2026-03-08 02:00 PST becomes 03:00 PDT; 2026-11-01 02:00 PDT becomes 01:00 PST.
    expect(todayKey(zone, new Date("2026-03-08T09:59:00Z"))).toBe("2026-03-08");
    expect(todayKey(zone, new Date("2026-03-09T06:59:00Z"))).toBe("2026-03-08");
    expect(todayKey(zone, new Date("2026-11-01T08:30:00Z"))).toBe("2026-11-01");
    expect(todayKey(zone, new Date("2026-11-02T07:59:00Z"))).toBe("2026-11-01");
    const mar = flat("2026-03-08").map((d) => d.date);
    expect(mar.filter((d) => d === "2026-03-08")).toHaveLength(1);
    const nov = flat("2026-11-01").map((d) => d.date);
    expect(nov.filter((d) => d === "2026-11-01")).toHaveLength(1);
  });
});

describe("addMonths", () => {
  it("moves by months across years and clamps the day", () => {
    expect(addMonths("2026-12-15", 1)).toBe("2027-01-15");
    expect(addMonths("2027-01-15", -1)).toBe("2026-12-15");
    expect(addMonths("2026-01-31", 1)).toBe("2026-02-28");
    expect(addMonths("2027-01-31", 1)).toBe("2027-02-28");
    expect(addMonths("2028-01-31", 1)).toBe("2028-02-29");
    expect(addMonths("2026-03-31", -12)).toBe("2025-03-31");
  });
});
