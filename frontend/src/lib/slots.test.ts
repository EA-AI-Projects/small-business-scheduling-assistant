import { describe, expect, it } from "vitest";

import { nextSlotOn, rangeFrom, slotAtFraction } from "./slots";

const la = "America/Los_Angeles";
// Boundary: slot snapping and prefill. Not covered: the server's own DST validation.
describe("slot prefill", () => {
  it("snaps a press to the 30-minute slot and defaults to one hour", () => {
    expect(slotAtFraction("2026-10-02", (10 * 60 + 20) / 1440, la))
      .toEqual({ start: "2026-10-02T10:00", end: "2026-10-02T11:00" });
    expect(slotAtFraction("2026-10-02", (10 * 60 + 30) / 1440, la).start).toBe("2026-10-02T10:30");
    expect(slotAtFraction("2026-10-02", 0, la).start).toBe("2026-10-02T00:00");
  });

  it("clamps the bottom edge and rolls a late end into the next day", () => {
    expect(slotAtFraction("2026-10-02", 1, la))
      .toEqual({ start: "2026-10-02T23:30", end: "2026-10-03T00:30" });
    expect(slotAtFraction("2026-10-02", -0.2, la).start).toBe("2026-10-02T00:00");
  });

  it("spring forward 2026-03-08: skips the missing 02:00 hour and ends one real hour later", () => {
    expect(rangeFrom("2026-03-08T01:00", la)).toEqual({ start: "2026-03-08T01:00", end: "2026-03-08T03:00" });
    expect(rangeFrom("2026-03-08T01:30", la)).toEqual({ start: "2026-03-08T01:30", end: "2026-03-08T03:30" });
    expect(rangeFrom("2026-03-08T02:00", la)).toEqual({ start: "2026-03-08T03:00", end: "2026-03-08T04:00" });
    expect(slotAtFraction("2026-03-08", (2 * 60 + 30) / 1440, la).start).toBe("2026-03-08T03:00");
    expect(rangeFrom("2026-03-08T03:00", la).end).toBe("2026-03-08T04:00");
  });

  it("fall back 2026-11-01: keeps wall-clock slots; the repeated hour stays on the wall clock", () => {
    expect(rangeFrom("2026-11-01T00:30", la)).toEqual({ start: "2026-11-01T00:30", end: "2026-11-01T01:30" });
    expect(rangeFrom("2026-11-01T02:00", la)).toEqual({ start: "2026-11-01T02:00", end: "2026-11-01T03:00" });
    expect(rangeFrom("2026-11-01T01:00", la)).toEqual({ start: "2026-11-01T01:00", end: "2026-11-01T02:00" });
  });

  it("next slot after now, at the DST boundary", () => {
    // 01:45 PST, 15 minutes before the clocks jump: the next slot is 03:00 PDT.
    expect(nextSlotOn("2026-03-08", new Date("2026-03-08T09:45:00Z"), la))
      .toEqual({ start: "2026-03-08T03:00", end: "2026-03-08T04:00" });
    // 03:00 PDT exactly: the next slot is 03:30.
    expect(nextSlotOn("2026-03-08", new Date("2026-03-08T10:00:00Z"), la).start).toBe("2026-03-08T03:30");
    // 00:45 PDT on fall back: next slot 01:00 (repeated hour; wall clock).
    expect(nextSlotOn("2026-11-01", new Date("2026-11-01T07:45:00Z"), la).start).toBe("2026-11-01T01:00");
    // 01:15 PST (second pass of the repeated hour): next slot 01:30.
    expect(nextSlotOn("2026-11-01", new Date("2026-11-01T09:15:00Z"), la).start).toBe("2026-11-01T01:30");
    // 23:50 local: the next slot is tomorrow 00:00, never in the past.
    expect(nextSlotOn("2026-10-02", new Date("2026-10-03T06:50:00Z"), la))
      .toEqual({ start: "2026-10-03T00:00", end: "2026-10-03T01:00" });
    // 23:15 local: still today at 23:30.
    expect(nextSlotOn("2026-10-02", new Date("2026-10-03T06:15:00Z"), la).start).toBe("2026-10-02T23:30");
  });

  it("uses 09:00 on a day other than today, and the business zone's today", () => {
    expect(nextSlotOn("2026-10-05", new Date("2026-10-02T20:00:00Z"), la))
      .toEqual({ start: "2026-10-05T09:00", end: "2026-10-05T10:00" });
    expect(nextSlotOn("2026-10-02", new Date("2026-10-03T03:10:00Z"), la).start).toBe("2026-10-02T20:30");
  });
});
