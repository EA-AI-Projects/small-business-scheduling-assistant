import { describe, expect, it } from "vitest";

import type { CalendarEvent } from "@/api/types";

import { layoutDay } from "./calendarLayout";

// Boundary: mapping events to per-day clipped positions and lanes. Not covered: rendering.
const la = "America/Los_Angeles";
const event = (id: string, start_at: string, end_at: string): CalendarEvent => ({
  event_id: id, start_at, end_at, status: "CONFIRMED", hold_expires_at: null, buffer_minutes: 0,
  duration_minutes: null });
const shape = (items: ReturnType<typeof layoutDay>) =>
  items.map((i) => [i.event.event_id, i.startMinute, i.endMinute, i.lane, i.lanes]);

describe("layoutDay", () => {
  it("places an event by business-local time", () => {
    // 17:00Z is 10:00 PDT.
    const items = layoutDay([event("a", "2026-10-02T17:00:00Z", "2026-10-02T19:00:00Z")], "2026-10-02", la);
    expect(shape(items)).toEqual([["a", 600, 720, 0, 1]]);
  });

  it("clips a midnight-crossing event to each day and drops it from other days", () => {
    const e = event("a", "2026-10-03T05:00:00Z", "2026-10-03T08:00:00Z"); // 22:00 -> 01:00 PDT
    const first = layoutDay([e], "2026-10-02", la);
    const second = layoutDay([e], "2026-10-03", la);
    expect(shape(first)).toEqual([["a", 1320, 1440, 0, 1]]);
    expect(first[0]?.continuesAfter).toBe(true);
    expect(shape(second)).toEqual([["a", 0, 60, 0, 1]]);
    expect(second[0]?.continuesBefore).toBe(true);
    expect(layoutDay([e], "2026-10-04", la)).toEqual([]);
  });

  it("does not show an event ending exactly at local midnight on the next day", () => {
    const e = event("a", "2026-10-03T05:00:00Z", "2026-10-03T07:00:00Z"); // 22:00 -> 00:00 PDT
    expect(shape(layoutDay([e], "2026-10-02", la))).toEqual([["a", 1320, 1440, 0, 1]]);
    expect(layoutDay([e], "2026-10-03", la)).toEqual([]);
  });

  it("positions by wall clock across the spring-forward change", () => {
    // 2026-03-08: 09:00 PDT = 16:00Z; 01:00 PST = 09:00Z.
    const items = layoutDay([event("a", "2026-03-08T16:00:00Z", "2026-03-08T17:00:00Z"),
      event("b", "2026-03-08T09:00:00Z", "2026-03-08T09:30:00Z")], "2026-03-08", la);
    expect(shape(items)).toEqual([["b", 60, 90, 0, 1], ["a", 540, 600, 0, 1]]);
  });

  it("handles the fall-back day, including the repeated hour", () => {
    // 2026-11-01: 09:00 PST = 17:00Z. 01:30 PDT = 08:30Z to 01:30 PST = 09:30Z (60 real minutes).
    const items = layoutDay([event("a", "2026-11-01T17:00:00Z", "2026-11-01T18:00:00Z"),
      event("r", "2026-11-01T08:30:00Z", "2026-11-01T09:30:00Z")], "2026-11-01", la);
    expect(shape(items)).toEqual([["r", 90, 150, 0, 1], ["a", 540, 600, 0, 1]]);
  });

  it("lays overlapping items side by side and reuses lanes after the group ends", () => {
    const items = layoutDay([
      event("a", "2026-10-02T17:00:00Z", "2026-10-02T19:00:00Z"),
      event("b", "2026-10-02T18:00:00Z", "2026-10-02T20:00:00Z"),
      event("c", "2026-10-02T19:00:00Z", "2026-10-02T21:00:00Z"),
      event("d", "2026-10-02T23:00:00Z", "2026-10-03T00:00:00Z"),
    ], "2026-10-02", la);
    expect(shape(items)).toEqual([
      ["a", 600, 720, 0, 2], ["b", 660, 780, 1, 2], ["c", 720, 840, 0, 2], ["d", 960, 1020, 0, 1]]);
  });

  it("gives short items a minimum extent that counts for overlap", () => {
    const items = layoutDay([event("a", "2026-10-02T17:00:00Z", "2026-10-02T17:05:00Z"),
      event("b", "2026-10-02T17:15:00Z", "2026-10-02T18:00:00Z")], "2026-10-02", la);
    expect(shape(items)).toEqual([["a", 600, 630, 0, 2], ["b", 615, 660, 1, 2]]);
  });
});
