// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { OwnerApi } from "@/lib/api";
import { OwnerProvider, useOwner } from "@/owner/OwnerContext";

import { ExceptionForm } from "../settings/ExceptionForm";
import { ScheduleTab } from "./ScheduleTab";

let DAY = "2026-10-07";
const NORMAL = Object.fromEntries([0, 1, 2, 3, 4, 5, 6].map((d) => [String(d), [{ opens: "08:00:00", closes: "17:00:00" }]]));
const base = {
  timezone: "America/Los_Angeles", weekly_windows: {}, booking_horizon_days: 14, slot_increment_minutes: 15,
  maximum_visit_minutes: 180, minimum_visit_gap_minutes: 30, opening_buffer_minutes: 0,
  closing_buffer_minutes: 0, hold_minutes: 1440, maximum_buffer_minutes: 30, holiday_calendar: null,
};
let saved: Record<string, { opens: string; closes: string }[]> = {};
let version = 1;
let weekly: Record<string, { opens: string; closes: string }[]> = NORMAL;

const api = {
  get: vi.fn(async (path: string) => {
    if (path === "/calendar") return { revision: 3, events: [] };
    if (path === "/policy") return { record: { version, policy: { ...base, weekly_windows: weekly, date_exceptions: saved } }, calendar_revision: 3 };
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
    saved = {}; version = 1; weekly = NORMAL; DAY = "2026-10-07";
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
    expect(host.querySelectorAll("[data-event-id]")).toHaveLength(0);
  });

  const labels = () => [...host.querySelectorAll("[data-date-exception]")].map((n) => n.getAttribute("aria-label"));

  it("shades only normal hours outside the shortened hours, starting with the visible text", async () => {
    await save("custom");
    expect(labels()).toEqual([
      "Closed (date exception hours), 8:00 AM–9:00 AM, Wednesday, Oct 7",
      "Closed (date exception hours), 12:00 PM–5:00 PM, Wednesday, Oct 7"]);
  });

  it("shades the same way on the DST fall-back and spring-forward Sundays", async () => {
    for (const day of ["2026-11-01", "2027-03-14"]) {
      DAY = day;
      await act(async () => host.querySelector<HTMLButtonElement>("button")!.click());
      await save("custom");
      expect(labels().map((l) => l?.split(", ").slice(1, 2)[0])).toEqual(["8:00 AM–9:00 AM", "12:00 PM–5:00 PM"]);
      await save("normal");
    }
  });

  it("draws nothing when the exception opens a normally closed day", async () => {
    saved = {};
    DAY = "2026-10-10"; // Saturday
    weekly = {};
    await act(async () => host.querySelector<HTMLButtonElement>("button")!.click());
    await save("custom");
    expect(labels()).toEqual([]);
  });

  it("is not an appointment or block: no event id and pressing it selects nothing", async () => {
    await save("closed");
    const note = host.querySelector<HTMLElement>("[data-date-exception]")!;
    expect(note.hasAttribute("data-event-id")).toBe(false);
    await act(async () => note.click());
    expect(host.ownerDocument.querySelector("[role='dialog']")).toBeNull();
  });

  it("shades nothing for a date without an exception, such as a federal holiday", async () => {
    DAY = "2026-11-11"; // Veterans Day: closed by holiday calendar, not an exception
    await act(async () => host.querySelector<HTMLButtonElement>("button")!.click());
    expect(labels()).toEqual([]);
  });

  it("shades each gap between several open windows on one day", async () => {
    saved = { [DAY]: [{ opens: "09:00:00", closes: "10:00:00" }, { opens: "13:00:00", closes: "15:00:00" }] };
    const refresh = [...host.querySelectorAll("button")].find((b) => b.textContent === "Refresh")!;
    await act(async () => refresh.click());
    expect(labels().map((l) => l?.split(", ")[1])).toEqual(
      ["8:00 AM–9:00 AM", "10:00 AM–1:00 PM", "3:00 PM–5:00 PM"]);
  });

  it("removes the closed time when normal hours are restored", async () => {
    await save("closed");
    await save("normal");
    expect(host.querySelector("[data-date-exception]")).toBeNull();
  });
});
