import { datesForView, dayKey, localInput, localTime } from "./time";

const ZONE = "America/Los_Angeles";

describe("business timezone helpers", () => {
  it("keys an instant by its local business date", () => {
    // 06:30 UTC on July 7 is still July 6 in Los Angeles.
    expect(dayKey("2026-07-07T06:30:00Z", ZONE)).toBe("2026-07-06");
    expect(localTime("2026-07-06T16:00:00Z", ZONE)).toBe("9:00 AM");
  });

  it("formats datetime-local values in the business timezone", () => {
    expect(localInput("2026-07-06T16:05:00Z", ZONE)).toBe("2026-07-06T09:05");
    expect(localInput("2026-01-06T00:00:00Z", ZONE)).toBe("2026-01-05T16:00");
  });

  it("returns a single day or the Monday-to-Sunday week", () => {
    expect(datesForView("2026-07-08", "day")).toEqual(["2026-07-08"]);
    expect(datesForView("2026-07-08", "week")).toEqual([
      "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"]);
    expect(datesForView("2026-07-12", "week")[0]).toBe("2026-07-06");
    expect(datesForView("", "day")).toEqual([]);
  });
});
