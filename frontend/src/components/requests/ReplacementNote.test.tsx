// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";

import type { Appointment } from "@/api/types";

import { ReplacementNote } from "./ReplacementNote";

const base = {
  client_id: "synthetic-client", duration_minutes: 60, version: 1,
} as Appointment;
const replacement = {
  ...base, appointment_id: "new", replaces_appointment_id: "old",
  start_at: "2026-07-08T16:00:00Z", end_at: "2026-07-08T17:00:00Z",
} as Appointment;
const original = {
  ...base, appointment_id: "old", replaces_appointment_id: null,
  start_at: "2026-07-06T16:00:00Z", end_at: "2026-07-06T17:00:00Z",
} as Appointment;

async function render(node: React.ReactNode) {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement("div");
  await act(async () => createRoot(host).render(node));
  return host.textContent ?? "";
}

describe("ReplacementNote", () => {
  it("names the pending original and says approval resolves both", async () => {
    const text = await render(<ReplacementNote request={replacement} original={original} originalConfirmed={false} zone="UTC" />);
    expect(text).toContain("original request for");
    expect(text).toContain("2026-07-06 · 4:00 PM");
    expect(text).toContain("is also pending");
    expect(text).toContain("resolves both requests");
    expect(text).not.toContain("original visit stays");
  });

  it("keeps the confirmed-original wording", async () => {
    const text = await render(<ReplacementNote request={replacement} original={null} originalConfirmed zone="UTC" />);
    expect(text).toBe("Replacement request; original visit stays until approval.");
  });

  it("is neutral when the original is no longer pending or confirmed", async () => {
    const text = await render(<ReplacementNote request={replacement} original={null}
      originalConfirmed={false} zone="UTC" />);
    expect(text).toContain("no longer pending");
    expect(text).toContain("confirms only this replacement");
    expect(text).not.toContain("original visit stays");
  });

  it("shows nothing for an ordinary request", async () => {
    const text = await render(<ReplacementNote request={original} original={null} originalConfirmed={false} zone="UTC" />);
    expect(text).toBe("");
  });
});
