import type { AvailabilityPolicy } from "@/api/types";

import { closedRanges } from "./dateExceptions";

const policy = { date_exceptions: {
  "2026-10-07": [],
  "2026-10-08": [{ opens: "09:00:00", closes: "12:00:00" }],
  "2026-10-09": [{ opens: "08:00:00", closes: "10:00:00" }, { opens: "13:00:00", closes: "17:00:00" }],
}, holiday_calendar: "US_FEDERAL" } as unknown as AvailabilityPolicy;

describe("closedRanges", () => {
  it("closes the whole date for a closed exception", () => {
    expect(closedRanges(policy, "2026-10-07")).toEqual([{ startMinute: 0, endMinute: 1440, wholeDay: true }]);
  });
  it("closes the time outside shortened hours", () => {
    expect(closedRanges(policy, "2026-10-08")).toEqual([
      { startMinute: 0, endMinute: 540, wholeDay: false },
      { startMinute: 720, endMinute: 1440, wholeDay: false }]);
    expect(closedRanges(policy, "2026-10-09").map((r) => [r.startMinute, r.endMinute]))
      .toEqual([[0, 480], [600, 780], [1020, 1440]]);
  });
  it("shows nothing for dates without an exception, including federal holidays", () => {
    expect(closedRanges(policy, "2026-11-11")).toEqual([]);
    expect(closedRanges(null, "2026-10-07")).toEqual([]);
  });
});
