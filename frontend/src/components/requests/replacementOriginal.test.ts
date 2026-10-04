import type { Appointment, CalendarEvent } from "@/api/types";

import { replacementOriginal } from "./replacementOriginal";

const NOW = Date.parse("2026-07-01T12:00:00Z");
const replacement = { appointment_id: "new", replaces_appointment_id: "old" } as Appointment;
const pending = (hold: string | null) =>
  ({ appointment_id: "old", status: "PENDING_APPROVAL", hold_expires_at: hold }) as Appointment;
const event = (status: CalendarEvent["status"]) => ({ event_id: "old", status }) as CalendarEvent;

describe("replacementOriginal", () => {
  it("treats a pending original with a live hold as pending", () => {
    const result = replacementOriginal(replacement, [pending("2026-07-02T12:00:00Z")], [], NOW);
    expect(result.original?.appointment_id).toBe("old");
    expect(result.originalConfirmed).toBe(false);
  });

  it("does not treat an original whose hold has expired as pending", () => {
    const result = replacementOriginal(replacement, [pending("2026-06-30T12:00:00Z")], [], NOW);
    expect(result).toEqual({ original: null, originalConfirmed: false });
  });

  it("recognizes a confirmed original from calendar events", () => {
    const result = replacementOriginal(replacement, [], [event("CONFIRMED")], NOW);
    expect(result).toEqual({ original: null, originalConfirmed: true });
  });

  it("gives neither for a declined or missing original", () => {
    expect(replacementOriginal(replacement, [], [event("DECLINED")], NOW))
      .toEqual({ original: null, originalConfirmed: false });
    expect(replacementOriginal(replacement, [], [], NOW))
      .toEqual({ original: null, originalConfirmed: false });
  });
});
