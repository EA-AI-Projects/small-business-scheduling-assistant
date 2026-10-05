// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { OwnerApi } from "@/lib/api";
import { OwnerProvider, useOwner } from "@/owner/OwnerContext";

import { ExceptionForm } from "../settings/ExceptionForm";
import { ScheduleTab } from "./ScheduleTab";

const DAY = "2026-10-07";
const base = {
  timezone: "America/Los_Angeles", weekly_windows: {}, booking_horizon_days: 14, slot_increment_minutes: 15,
  maximum_visit_minutes: 180, minimum_visit_gap_minutes: 30, opening_buffer_minutes: 0,
  closing_buffer_minutes: 0, hold_minutes: 1440, maximum_buffer_minutes: 30, holiday_calendar: null,
};
let saved: Record<string, { opens: string; closes: string }[]> = {};
let version = 1;

const api = {
  get: vi.fn(async (path: string) => {
    if (path === "/calendar") return { revision: 3, events: [] };
    if (path === "/policy") return { record: { version, policy: { ...base, date_exceptions: saved } }, calendar_revision: 3 };
    return [];
  }),
  request: vi.fn(async (_path: string, options: { body: { policy: { date_exceptions: typeof saved } } }) => {
    saved = options.body.policy.date_exceptions;
    version += 1;
  }),
} as unknown as OwnerApi;

function Controls() {
  const { goToDate, loaded } = useOwner();
  return <>
    <button type="button" disabled={!loaded} onClick={() => goToDate(DAY)}>go</button>
    <ExceptionForm />
  </>;
}

describe("Schedule date exceptions", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(async () => {
    saved = {}; version = 1;
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root.render(
      <OwnerProvider api={api} notify={() => undefined}><Controls /><ScheduleTab /></OwnerProvider>));
    await act(async () => host.querySelector<HTMLButtonElement>("button")!.click());
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

  async function save(mode: string) {
    const form = host.querySelector<HTMLFormElement>("form[aria-label='Date exception']")!;
    const set = (name: string, value: string) => {
      const field = form.elements.namedItem(name) as HTMLInputElement | HTMLSelectElement;
      Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), "value")!.set!.call(field, value);
    };
    set("date", DAY); set("mode", mode); set("opens", "09:00"); set("closes", "12:00");
    await act(async () => form.requestSubmit());
  }

  it("shows no exception time before one is saved", () => {
    expect(host.querySelector("[data-date-exception]")).toBeNull();
  });

  it("shows a saved closed date on the Schedule without a manual reload", async () => {
    await save("closed");
    const note = host.querySelector("[data-date-exception]");
    expect(note?.getAttribute("aria-label")).toContain("Closed (date exception)");
    expect(note?.getAttribute("aria-label")).toContain("Wednesday");
    expect(note?.tagName).toBe("DIV"); // not an appointment or block button
    expect(host.querySelectorAll("[data-event-id]")).toHaveLength(0);
  });

  it("shows only the time outside shortened hours", async () => {
    await save("custom");
    const labels = [...host.querySelectorAll("[data-date-exception]")].map((n) => n.getAttribute("aria-label"));
    expect(labels).toHaveLength(2);
    expect(labels[0]).toContain("12:00 AM to 9:00 AM");
    expect(labels[1]).toContain("12:00 PM to midnight");
  });

  it("removes the closed time when normal hours are restored", async () => {
    await save("closed");
    await save("normal");
    expect(host.querySelector("[data-date-exception]")).toBeNull();
  });
});
