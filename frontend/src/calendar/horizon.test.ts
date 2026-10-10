import { pastLimit, periodStart } from "./horizon";

describe("pastLimit", () => {
  it("never limits without a maximum date", () => {
    expect(pastLimit("2030-01-01", "day", null)).toBe(false);
    expect(pastLimit("2030-01-01", "year", undefined)).toBe(false);
  });
  it("limits only periods that start after the maximum, so a straddling period may show", () => {
    expect(pastLimit("2026-10-25", "day", "2026-10-24")).toBe(true);
    expect(pastLimit("2026-10-24", "day", "2026-10-24")).toBe(false);
    expect(pastLimit("2026-10-28", "month", "2026-10-24")).toBe(false);
    expect(pastLimit("2026-11-02", "month", "2026-10-24")).toBe(true);
    expect(pastLimit("2026-12-30", "year", "2026-10-24")).toBe(false);
    expect(periodStart("2026-10-28", "week")).toBe("2026-10-26");
  });
});
