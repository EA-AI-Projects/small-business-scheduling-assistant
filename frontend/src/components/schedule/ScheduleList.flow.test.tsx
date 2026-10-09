// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { OwnerApi } from "@/lib/api";
import { OwnerProvider, useOwner } from "@/owner/OwnerContext";

import { ScheduleTab } from "./ScheduleTab";

const makeEvent = (id: string, start_at: string, status: "CONFIRMED" | "PENDING_APPROVAL") => ({
  event_id: id, start_at, end_at: new Date(Date.parse(start_at) + 3_600_000).toISOString(),
  status, hold_expires_at: null, buffer_minutes: 0, duration_minutes: 60,
});

const events = [
  makeEvent("confirmed", "2026-10-08T16:00:00Z", "CONFIRMED"),
  makeEvent("pending", "2026-10-08T17:00:00Z", "PENDING_APPROVAL"),
  makeEvent("later", "2026-11-07T17:00:00Z", "CONFIRMED"),
];

const api = {
  get: vi.fn(async (path: string) => {
    if (path === "/calendar") return { business_id: "pilot", revision: 1, events };
    if (path === "/requests") return [{ appointment_id: "pending", client_id: "pending-client" }];
    if (path === "/clients") return [
      { client_id: "confirmed-client", name: "Example Client" },
      { client_id: "pending-client", name: "Sample Client" },
    ];
    if (path === "/policy") return { record: { policy: { timezone: "America/Los_Angeles" } } };
    if (path === "/booking-outreach") return {};
    if (path.startsWith("/appointments/")) return {
      appointment_id: path.slice("/appointments/".length), client_id: "confirmed-client",
      start_at: events[0]!.start_at, end_at: events[0]!.end_at, status: "CONFIRMED",
    };
    throw new Error(`Unexpected path: ${path}`);
  }),
} as unknown as OwnerApi;

function Controls() {
  const { loaded, goToDate, setView, stepRange } = useOwner();
  return <>
    <button type="button" disabled={!loaded} onClick={() => { goToDate("2026-10-08"); setView("schedule"); }}>Schedule</button>
    <button type="button" onClick={() => stepRange(1)}>Next</button>
  </>;
}

describe("Schedule agenda owner flow", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(async () => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}>
      <Controls /><ScheduleTab />
    </OwnerProvider>));
    await act(async () => host.querySelector<HTMLButtonElement>("button")!.click());
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

  it("shows names, opens an item card, and extends then steps by the loaded range", async () => {
    const rows = () => [...host.querySelectorAll<HTMLButtonElement>(".schedule-item")];
    expect(rows()).toHaveLength(2);
    expect(rows()[0]?.getAttribute("aria-label")).toContain("Example Client");
    expect(rows()[1]?.getAttribute("aria-label")).toContain("Sample Client");
    await act(async () => rows()[0]?.click());
    expect(document.querySelector("[role='dialog']")).not.toBeNull();
    await act(async () => host.querySelector<HTMLButtonElement>(".schedule-load-more")!.click());
    expect(rows()).toHaveLength(3);
    await act(async () => host.querySelectorAll<HTMLButtonElement>("button")[1]?.click());
    expect(rows()).toHaveLength(0); // Next starts after the expanded 60-day range.
    expect(host.textContent).toContain("No confirmed visits or pending requests in this range");
  });
});
