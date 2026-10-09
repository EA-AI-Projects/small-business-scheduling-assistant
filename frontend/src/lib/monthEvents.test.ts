import { describe, expect, it } from "vitest";

import type { CalendarEvent } from "@/api/types";

import { monthChipTime, monthEvents } from "./monthEvents";
import { monthWeeks } from "./monthGrid";
import { rangeTitle, stepDate } from "./time";

const event = (event_id: string, start_at: string, end_at: string): CalendarEvent => ({
  event_id, start_at, end_at, status: "CONFIRMED", hold_expires_at: null,
  buffer_minutes: 0, duration_minutes: 60,
});

describe("Month calendar range and placement", () => {
  it("uses Monday-first 5 or 6 week ranges and steps through months", () => {
    const october = monthWeeks("2026-10-09");
    expect(october).toHaveLength(5);
    expect(october[0]?.[0]?.date).toBe("2026-09-28");
    expect(october[4]?.[6]?.date).toBe("2026-11-01");
    expect(monthWeeks("2026-08-09")).toHaveLength(6);
    expect(stepDate("2026-01-31", "month", 1)).toBe("2026-02-28");
    expect(rangeTitle("2026-10-09", "month")).toBe("October 2026");
  });

  it("places events by business-local day across month boundaries and both DST changes", () => {
    const zone = "America/Los_Angeles";
    const events = [
      event("prev", "2026-09-29T06:30:00Z", "2026-09-29T07:30:00Z"),
      event("spring", "2026-03-08T09:30:00Z", "2026-03-08T10:30:00Z"),
      event("fall", "2026-11-01T08:30:00Z", "2026-11-01T09:30:00Z"),
      event("next", "2026-11-02T08:30:00Z", "2026-11-02T09:30:00Z"),
      event("outside", "2026-12-15T18:00:00Z", "2026-12-15T19:00:00Z"),
    ];
    const october = monthEvents(events, "2026-10-09", zone);
    expect(october.get("2026-09-28")?.map((item) => item.event.event_id)).toEqual(["prev"]);
    expect(october.get("2026-11-01")?.map((item) => item.event.event_id)).toEqual(["fall"]);
    expect([...october.values()].flat().some((item) => item.event.event_id === "outside")).toBe(false);
    expect(monthEvents(events, "2026-03-09", zone).get("2026-03-08")?.map((item) => item.event.event_id))
      .toEqual(["spring"]);
    expect(monthEvents(events, "2026-11-09", zone).get("2026-11-02")?.map((item) => item.event.event_id))
      .toEqual(["next"]);
  });

  it("shows midnight and a continuation on the second day of an overnight event", () => {
    const zone = "America/Los_Angeles";
    const overnight = event("overnight", "2026-11-02T07:00:00Z", "2026-11-02T09:00:00Z");
    const days = monthEvents([overnight], "2026-11-02", zone);
    const first = days.get("2026-11-01")?.[0];
    const next = days.get("2026-11-02")?.[0];
    expect(first && monthChipTime(first, zone)).toBe("11:00 PM");
    expect(first?.continuesAfter).toBe(true);
    expect(next && monthChipTime(next, zone)).toBe("12:00 AM");
    expect(next?.continuesBefore).toBe(true);
  });
});
