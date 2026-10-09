// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { CalendarEvent } from "@/api/types";
import type { OwnerApi } from "@/lib/api";
import { rangeTitle, stepDate } from "@/lib/time";
import { OwnerProvider, useOwner } from "@/owner/OwnerContext";

import { ScheduleTab } from "./ScheduleTab";
import { YearGrid } from "./YearGrid";

const event = (id: string, start_at: string, end_at: string): CalendarEvent => ({
  event_id: id, start_at, end_at, status: "CONFIRMED", hold_expires_at: null,
  buffer_minutes: 0, duration_minutes: 60,
});

describe("Year calendar owner flow", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

  it("shows all months, counts local-day items, and opens the chosen day", async () => {
    const onDay = vi.fn();
    await act(async () => root.render(<YearGrid date="2028-01-01" zone="America/Los_Angeles"
      events={[
        event("leap", "2028-02-29T18:00:00Z", "2028-02-29T19:00:00Z"),
        event("second", "2028-02-29T20:00:00Z", "2028-02-29T21:00:00Z"),
        event("year-end", "2029-01-01T07:00:00Z", "2029-01-01T09:00:00Z"),
      ]} onNavigate={() => undefined} onDay={onDay} />));
    expect(host.querySelectorAll(".year-month")).toHaveLength(12);
    expect(host.querySelectorAll(".year-month [role='grid']")).toHaveLength(12);
    expect(host.querySelector('[data-date="2028-02-29"]')?.getAttribute("aria-label")).toContain("2 items");
    expect(host.querySelector('[data-date="2028-12-31"]')?.getAttribute("aria-label")).toContain("1 item");
    expect(host.querySelector('[data-date="2028-01-01"]')?.getAttribute("aria-label")).toContain("0 items");
    await act(async () => host.querySelector<HTMLButtonElement>('[data-date="2028-02-29"]')!.click());
    expect(onDay).toHaveBeenCalledWith("2028-02-29");
  });

  it("moves focus across months and navigates across the year boundary", async () => {
    const onNavigate = vi.fn();
    const render = (date: string) => root.render(<YearGrid date={date} zone="UTC" events={[]}
      onNavigate={onNavigate} onDay={() => undefined} />);
    await act(async () => render("2028-12-31"));
    const last = host.querySelector<HTMLButtonElement>('[data-date="2028-12-31"]')!;
    last.focus();
    await act(async () => last.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
    expect(onNavigate).toHaveBeenCalledWith("2029-01-01");
    await act(async () => render("2029-01-01"));
    expect(document.activeElement?.getAttribute("data-date")).toBe("2029-01-01");
    expect(stepDate("2028-02-29", "year", -1)).toBe("2027-02-28");
    expect(stepDate("2028-02-29", "year", 1)).toBe("2029-02-28");
    expect(rangeTitle("2028-02-29", "year")).toBe("2028");
  });

  it("uses the loaded owner snapshot when navigating to another year", async () => {
    const get = vi.fn(async (path: string) => {
      if (path === "/calendar") return { business_id: "pilot", revision: 1, events: [
        event("leap", "2028-02-29T18:00:00Z", "2028-02-29T19:00:00Z"),
        event("next", "2029-03-01T18:00:00Z", "2029-03-01T19:00:00Z"),
      ] };
      if (path === "/requests" || path === "/clients") return [];
      if (path === "/policy") return { record: { policy: { timezone: "America/Los_Angeles" } } };
      if (path === "/booking-outreach") return {};
      throw new Error(`Unexpected path: ${path}`);
    });
    function Controls() {
      const { loaded, goToDate, setView, stepRange } = useOwner();
      return <>
        <button type="button" disabled={!loaded} onClick={() => { goToDate("2028-02-29"); setView("year"); }}>Year</button>
        <button type="button" onClick={() => stepRange(1)}>Next year</button>
      </>;
    }
    await act(async () => root.render(<OwnerProvider api={{ get } as unknown as OwnerApi} notify={() => undefined}>
      <Controls /><ScheduleTab />
    </OwnerProvider>));
    await act(async () => host.querySelector<HTMLButtonElement>("button")!.click());
    expect(host.querySelector('[data-date="2028-02-29"]')?.getAttribute("aria-label")).toContain("1 item");
    await act(async () => host.querySelectorAll<HTMLButtonElement>("button")[1]!.click());
    expect(host.querySelector('[data-date="2029-03-01"]')?.getAttribute("aria-label")).toContain("1 item");
    expect(get.mock.calls.filter(([path]) => path === "/calendar")).toHaveLength(1);
  });
});
