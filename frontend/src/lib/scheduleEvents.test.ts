import { describe, expect, it } from "vitest";

import type { CalendarEvent } from "@/api/types";
import { scheduleEvents } from "./scheduleEvents";

const event = (id: string, start_at: string, status: CalendarEvent["status"] = "CONFIRMED"): CalendarEvent => ({
  event_id: id, start_at, end_at: new Date(Date.parse(start_at) + 60 * 60_000).toISOString(),
  status, hold_expires_at: null, buffer_minutes: 0, duration_minutes: 60,
});

describe("Schedule range in the business timezone", () => {
  it("groups and sorts events through spring DST, excluding range end and other statuses", () => {
    const groups = scheduleEvents([
      event("later", "2026-03-08T10:30:00Z"), // 3:30 AM PDT
      event("before", "2026-03-08T07:30:00Z"), // March 7, 11:30 PM PST
      event("early", "2026-03-08T09:30:00Z", "PENDING_APPROVAL"), // 1:30 AM PST
      event("out", "2026-03-10T07:00:00Z"), // March 10, midnight PDT
      event("block", "2026-03-09T16:00:00Z", "UNAVAILABLE"),
    ], "2026-03-08", 2, "America/Los_Angeles", new Set(["early"]));
    expect(groups.map((group) => [group.date, group.events.map((item) => item.event_id)]))
      .toEqual([["2026-03-08", ["early", "later"]]]);
  });

  it("keeps both repeated fall DST hour entries in chronological order", () => {
    const groups = scheduleEvents([
      event("second", "2026-11-01T09:30:00Z"),
      event("first", "2026-11-01T08:30:00Z"),
    ], "2026-11-01", 30, "America/Los_Angeles");
    expect(groups[0]?.events.map((item) => item.event_id)).toEqual(["first", "second"]);
  });

  it("omits pending holds that are no longer current requests", () => {
    const groups = scheduleEvents([
      event("stale", "2026-11-01T17:00:00Z", "PENDING_APPROVAL"),
      event("current", "2026-11-01T18:00:00Z", "PENDING_APPROVAL"),
    ], "2026-11-01", 30, "America/Los_Angeles", new Set(["current"]));
    expect(groups[0]?.events.map((item) => item.event_id)).toEqual(["current"]);
  });
});
