// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { useCalendarState } from "@/calendar/useCalendarState";
import { ClientHome } from "@/components/client/ClientHome";
import { CalendarControls } from "@/components/shell/CalendarControls";
import { parseConfig } from "@/lib/config";
import { addDays, rangeAnnouncement, rangeTitle, todayKey } from "@/lib/time";

const config = parseConfig({ authMode: "cognito", apiBaseUrl: "https://api.example.test",
  businessId: "pilot", cognitoDomain: "https://auth.example.test", clientId: "public" });
// A zone unlike the Pacific business zone and unlikely to equal the test machine's zone.
const ZONE = "Pacific/Auckland";
// Saturday 2026-10-10 13:00 in Auckland.
const NOW = "2026-10-10T00:00:00Z";

function booking(id: string, status: string, start: string, replaces: string | null = null) {
  const end = new Date(Date.parse(start) + 7_200_000).toISOString();
  return { appointment_id: id, status, start_at: start, end_at: end, duration_minutes: 120,
    hold_expires_at: status === "PENDING_APPROVAL" ? "2026-10-10T06:00:00Z" : null, requested_at: null,
    version: 1, replaces_appointment_id: replaces };
}
const BOOKINGS = [
  booking("c", "CONFIRMED", "2026-10-12T01:00:00Z"), // Mon Oct 12, 2:00 PM
  booking("p", "PENDING_APPROVAL", "2026-10-13T01:00:00Z"), // Tue Oct 13
  booking("m", "PENDING_APPROVAL", "2026-10-14T01:00:00Z", "c"), // Wed Oct 14, moves "c"
  booking("f", "CONFIRMED", "2026-11-18T01:00:00Z"),
];
/** 10:00 AM Auckland (UTC+13 in October) on a local day, as an instant. */
const openStart = (day: string) => new Date(Date.parse(`${day}T10:00:00Z`) - 13 * 3_600_000).toISOString();
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

/** The Calendar page: the shell's header controls beside the page, sharing one calendar state. */
function Page({ horizon = 14 }: { horizon?: number | null }) {
  const maxDate = horizon === null ? null : addDays(todayKey(ZONE), horizon);
  const calendar = useCalendarState({ ready: true, zone: ZONE, maxDate });
  return <>
    <CalendarControls title={rangeTitle(calendar.date, calendar.view, calendar.scheduleDays)}
      announcement={rangeAnnouncement(calendar.date, calendar.view, calendar.scheduleDays)}
      view={calendar.view} date={calendar.date} today={todayKey(ZONE)} scheduleDays={calendar.scheduleDays} maxDate={maxDate}
      onToday={calendar.goToday} onPick={calendar.goToDate} onStep={calendar.stepRange} onView={calendar.setView} />
    <ClientHome config={config} token="access" zone={ZONE} horizonDays={horizon} calendar={calendar} onSessionEnded={() => undefined} />
  </>;
}

// Boundary: the client Calendar page over a stubbed client API. Not covered: sign-in, the real
// header, popover placement, or the request/move/cancel writes (see ClientHome.flow.test.tsx).
// These tests step through many views with the DOM re-rendering each time; the default 5s is tight on a busy machine.
const SLOW = 15_000;

describe("client calendar views", () => {
  let host: HTMLDivElement;
  let root: Root;
  let fetcher: ReturnType<typeof vi.fn>;
  beforeEach(async () => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.append(host); root = createRoot(host);
    vi.useFakeTimers({ toFake: ["Date"], now: new Date(NOW) });
    fetcher = vi.fn(async (url: string) => url.includes("availability")
      ? json({ day: "x", duration_minutes: 90, starts_at: [openStart(new URL(url).searchParams.get("day")!)] })
      : json({ bookings: BOOKINGS }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<Page />));
  });
  afterEach(async () => { vi.useRealTimers(); await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });

  const press = (label: string) => act(async () =>
    host.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!.click());
  const setView = (view: string) => act(async () => {
    const select = host.querySelector("select")!;
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, view);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const availabilityCalls = () => fetcher.mock.calls.filter(([url]) => String(url).includes("availability")).length;
  const eventLabels = () => Array.from(host.querySelectorAll("[data-event-id]")).map((node) => node.getAttribute("aria-label") ?? "");

  it("requests a Week start from its business-local column after an explicit popover press", async () => {
    const pendingStart = openStart("2026-10-11");
    fetcher.mockImplementation(async (url: string, init?: RequestInit) => init?.method === "POST"
      ? json(booking("new", "PENDING_APPROVAL", pendingStart))
      : url.includes("availability")
        ? json({ day: new URL(url).searchParams.get("day"), duration_minutes: 90,
          starts_at: [openStart(new URL(url).searchParams.get("day")!)] })
        : json({ bookings: [] }));
    await setView("week");
    const column = host.querySelector<HTMLElement>('[data-day="2026-10-11"]')!;
    const start = column.querySelector<HTMLButtonElement>('[data-available-start]')!;
    expect(start).toBeTruthy();
    expect(start.getAttribute("style")).toContain("41.666");
    expect(start.getAttribute("aria-label")).toContain("Sunday, Oct 11");
    expect(start.tabIndex).toBe(0);
    await act(async () => start.click());
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!;
    expect(dialog.textContent).toContain("2026-10-11 · 10:00 AM");
    expect(dialog.textContent).toContain(ZONE);
    expect(dialog.textContent).toContain("not confirmed until the owner approves");
    expect(fetcher.mock.calls.some((call) => (call[1] as RequestInit | undefined)?.method === "POST")).toBe(false);
    await act(async () => Array.from(dialog.querySelectorAll("button"))
      .find((button) => button.textContent?.includes("Send request for owner approval"))!.click());
    expect(fetcher.mock.calls.filter((call) => (call[1] as RequestInit | undefined)?.method === "POST")).toHaveLength(1);
    expect(host.textContent).toContain("waiting for owner approval");
  });

  it("keeps adjacent 15-minute starts distinct and reachable in Day and Week", async () => {
    fetcher.mockImplementation(async (url: string) => url.includes("availability")
      ? json({ day: new URL(url).searchParams.get("day"), duration_minutes: 90,
        starts_at: [openStart(new URL(url).searchParams.get("day")!),
          new Date(Date.parse(openStart(new URL(url).searchParams.get("day")!)) + 15 * 60_000).toISOString()] })
      : json({ bookings: [] }));
    await setView("week");
    const adjacent = (day: string) => [...host.querySelectorAll<HTMLButtonElement>(`[data-day="${day}"] [data-available-start]`)];
    const week = adjacent("2026-10-11");
    expect(week).toHaveLength(2);
    expect(week.map((slot) => slot.tabIndex)).toEqual([0, 0]);
    expect(week[0]!.style.top).toBe("41.66666666666667%");
    expect(week[1]!.style.top).toBe("42.70833333333333%");
    expect(week[0]!.style.left).not.toBe(week[1]!.style.left);
    await act(async () => week[1]!.click());
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain("10:15 AM");
    await act(async () => document.body.querySelector<HTMLButtonElement>('[aria-label="Close"]')!.click());
    await setView("day");
    const day = adjacent("2026-10-10");
    expect(day).toHaveLength(2);
    expect(day[0]!.style.left).not.toBe(day[1]!.style.left);
    await act(async () => day[0]!.click());
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain("10:00 AM");
  });

  it("switches views and dates, showing only the client's own bookings and open times in Day and Week", async () => {
    // Day (default): today has nothing booked, open times are offered, the legend names all three states.
    expect(host.textContent).toContain("You have no appointments or requests in this day");
    expect(host.querySelector("[data-available-start]")).not.toBeNull();
    expect(host.textContent).toContain("Move request (not confirmed)");
    expect(availabilityCalls()).toBe(1);

    // Week: this week has an available start; next week shows all three states, distinctly.
    await setView("week");
    expect(host.querySelector("[data-available-start]")).not.toBeNull();
    expect(availabilityCalls()).toBe(3);
    await press("Next week");
    const labels = eventLabels();
    expect(labels).toHaveLength(3);
    expect(labels.find((label) => label.includes("Monday"))).toContain("Confirmed");
    const pendingLabel = labels.find((label) => label.includes("Tuesday"))!;
    expect(pendingLabel).toContain("Waiting for approval (not confirmed)");
    expect(pendingLabel).not.toContain("Confirmed");
    const moveLabel = labels.find((label) => label.includes("Wednesday"))!;
    expect(moveLabel).toContain("Move request (not confirmed)");
    expect(moveLabel).not.toContain("Confirmed");
    expect(host.querySelector(".cal-event.confirmed")).not.toBeNull();
    expect(host.querySelector(".cal-event.pending")).not.toBeNull();
    expect(host.querySelector(".cal-event.move")).not.toBeNull();
    // Times show in the business zone, not the machine's: 14:00 Auckland.
    expect(labels.find((label) => label.includes("Monday"))).toContain("2:00 PM");

    // Tapping a weekday heading opens Day for that date, with its open times.
    await press("Tuesday, Oct 13, open Day view");
    expect(host.querySelector("[data-available-start]")).not.toBeNull();
    expect(eventLabels()).toHaveLength(1);
    expect(availabilityCalls()).toBe(11);

    // Month and tapping a day: chips only, no open times, then Day again.
    await setView("month");
    expect(host.querySelector("[data-available-start]")).toBeNull();
    expect(host.querySelectorAll(".month-item")).toHaveLength(3);
    await press("Monday, Oct 12, open Day view");
    expect(eventLabels()[0]).toContain("Confirmed");
    expect(host.querySelector("[data-available-start]")).not.toBeNull();

    // Schedule lists the upcoming items; Year counts them; neither offers times.
    await setView("schedule");
    expect(host.querySelectorAll(".schedule-item")).toHaveLength(3);
    expect(host.textContent).toContain("Your move request");
    expect(host.querySelector("[data-available-start]")).toBeNull();
    await setView("year");
    expect(host.querySelector("[data-available-start]")).toBeNull();
    // Browsing stops at the horizon: the year holding it is the last one.
    expect(host.querySelector<HTMLButtonElement>("[aria-label='Next year']")!.disabled).toBe(true);

    // Day and Week views asked only for dates in the horizon.
    expect(availabilityCalls()).toBe(12);
    expect(fetcher.mock.calls.every(([url]) => String(url).startsWith("https://api.example.test/v1/client/"))).toBe(true);
  }, SLOW);

  it("shows an empty calendar with a short note for past dates", async () => {
    await press("Previous day");
    expect(host.textContent).toContain("Past visits are not shown.");
    expect(host.querySelector("[data-available-start]")).toBeNull();
    expect(eventLabels()).toHaveLength(0);
    expect(availabilityCalls()).toBe(1);
  });
  it("stops browsing at the booking horizon and explains a day beyond it", async () => {
    // Today is Oct 10 in Auckland; the horizon day is Oct 24.
    for (let step = 0; step < 14; step += 1) await press("Next day");
    expect(host.querySelector("[data-available-start]")).not.toBeNull();
    const next = host.querySelector<HTMLButtonElement>("[aria-label='Next day']")!;
    expect(next.disabled).toBe(true);
    await act(async () => next.click());
    expect(host.textContent).toContain("October 24, 2026");
    expect(host.textContent).not.toContain("We only book up to");

    // Switching views at the edge does not crash; the period holding the horizon is the last one.
    await setView("week");
    expect(host.querySelector('[data-day="2026-10-24"] [data-available-start]')).not.toBeNull();
    expect(host.querySelector('[data-day="2026-10-25"] [data-available-start]')).toBeNull();
    for (const view of ["month", "schedule", "year", "day"]) await setView(view);
    await setView("month");
    expect(host.querySelector<HTMLButtonElement>("[aria-label='Next month']")!.disabled).toBe(true);
    await setView("year");
    expect(host.querySelector<HTMLButtonElement>("[aria-label='Next year']")!.disabled).toBe(true);
    await setView("week");
    expect(host.querySelector<HTMLButtonElement>("[aria-label='Next week']")!.disabled).toBe(true);
    await setView("schedule");
    expect(host.querySelector<HTMLButtonElement>("[aria-label='Next 30 days']")!.disabled).toBe(true);
    expect(host.textContent).not.toContain("Load more");

    // Today goes back; a month near the edge straddles the horizon and a day tapped past it says why.
    await press("Today");
    await setView("month");
    expect(host.querySelector<HTMLButtonElement>("[aria-label='Next month']")!.disabled).toBe(true);
    const asked = fetcher.mock.calls.map(([url]) => String(url));
    expect(asked.some((url) => url.endsWith("day=2026-10-25"))).toBe(false);
  }, SLOW);
  it("clamps a picked date past the horizon and shows the horizon message for a straddling month day", async () => {
    await press("Today");
    await press("Next day");
    await act(async () => host.querySelector<HTMLButtonElement>("[aria-label$='choose date']")!.click());
    await act(async () => document.querySelector<HTMLButtonElement>("[data-date='2026-11-05']")!.click());
    expect(host.textContent).toContain("October 24, 2026");
    expect(host.textContent).not.toContain("We only book up to");

    await setView("month");
    await press("Wednesday, Oct 28, open Day view");
    expect(host.textContent).toContain("We only book up to 14 days ahead.");
    expect(host.querySelector("[data-available-start]")).toBeNull();
    const asked = fetcher.mock.calls.map(([url]) => String(url));
    expect(asked.some((url) => url.endsWith("day=2026-10-25"))).toBe(false);
    expect(host.querySelector<HTMLButtonElement>("[aria-label='Next day']")!.disabled).toBe(true);
  });
  const pickDate = async (date: string) => {
    await act(async () => host.querySelector<HTMLButtonElement>("[aria-label$='choose date']")!.click());
    await act(async () => document.querySelector<HTMLButtonElement>(`[role='dialog'] [data-date='${date}']`)!.click());
  };
  it("keeps Month, Week and Year views at the horizon by arrows and by the picker", async () => {
    for (const view of ["month", "week", "year"] as const) {
      await press("Today");
      await setView(view);
      for (let step = 0; step < 3; step += 1) {
        const next = host.querySelector<HTMLButtonElement>(`[aria-label^='Next ${view}']`)!;
        if (!next.disabled) await act(async () => next.click());
      }
      expect(host.querySelector<HTMLButtonElement>(`[aria-label^='Next ${view}']`)!.disabled).toBe(true);
      expect(host.textContent).toContain(rangeTitle("2026-10-24", view, 30));
      // A pick well past the horizon lands on its last day, whatever the view.
      await pickDate("2026-11-05");
      expect(host.textContent).toContain(rangeTitle("2026-10-24", view, 30));
      expect(host.querySelector<HTMLButtonElement>(`[aria-label^='Next ${view}']`)!.disabled).toBe(true);
      expect(host.textContent).not.toContain("We only book up to");
    }
    expect(fetcher.mock.calls.map(([url]) => String(url)).some((url) => url.includes("day=2026-11"))).toBe(false);
  }, SLOW);
  it("limits nothing when the horizon is unknown", async () => {
    await act(async () => root.render(<Page horizon={null} />));
    await setView("year");
    const next = host.querySelector<HTMLButtonElement>("[aria-label='Next year']")!;
    expect(next.disabled).toBe(false);
    await act(async () => next.click());
    expect(host.textContent).toContain("2027");
    await pickDate("2027-11-05");
    expect(host.textContent).toContain("2027");
    await setView("day");
    expect(host.textContent).toContain("November 5, 2027");
    expect(host.textContent).not.toContain("We only book up to");
  });
  it("drops a chosen time when the date or view changes", async () => {
    const pick = () => act(async () => host.querySelector<HTMLButtonElement>("[data-available-start]")!.click());
    await pick();
    expect(document.body.querySelector("[role=dialog]")).not.toBeNull();
    await press("Next day");
    expect(document.body.querySelector("[role=dialog]")).toBeNull();
    await pick();
    expect(document.body.querySelector("[role=dialog]")).not.toBeNull();
    await setView("week");
    await setView("day");
    expect(document.body.querySelector("[role=dialog]")).toBeNull();
  });
});
