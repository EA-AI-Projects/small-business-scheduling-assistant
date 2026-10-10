// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { useCalendarState } from "@/calendar/useCalendarState";
import { ClientHome } from "@/components/client/ClientHome";
import { CalendarControls } from "@/components/shell/CalendarControls";
import { parseConfig } from "@/lib/config";
import { rangeAnnouncement, rangeTitle, todayKey } from "@/lib/time";

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
function Page() {
  const calendar = useCalendarState({ ready: true, zone: ZONE });
  return <>
    <CalendarControls title={rangeTitle(calendar.date, calendar.view, calendar.scheduleDays)}
      announcement={rangeAnnouncement(calendar.date, calendar.view, calendar.scheduleDays)}
      view={calendar.view} date={calendar.date} today={todayKey(ZONE)} scheduleDays={calendar.scheduleDays}
      onToday={calendar.goToday} onPick={calendar.goToDate} onStep={calendar.stepRange} onView={calendar.setView} />
    <ClientHome config={config} token="access" zone={ZONE} horizonDays={14} calendar={calendar} onSessionEnded={() => undefined} />
  </>;
}

// Boundary: the client Calendar page over a stubbed client API. Not covered: sign-in, the real
// header, popover placement, or the request/move/cancel writes (see ClientHome.flow.test.tsx).
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

  it("switches views and dates, showing only the client's own bookings and open times only in Day", async () => {
    // Day (default): today has nothing booked, open times are offered, the legend names all three states.
    expect(host.textContent).toContain("You have no appointments or requests in this day");
    expect(host.querySelector("[aria-label='Available start times']")).not.toBeNull();
    expect(host.textContent).toContain("Move request (not confirmed)");
    expect(availabilityCalls()).toBe(1);

    // Week: this week is empty; next week shows all three states, distinctly, with no open times.
    await setView("week");
    expect(host.querySelector("[aria-label='Available start times']")).toBeNull();
    await press("Next week");
    const labels = eventLabels();
    expect(labels).toHaveLength(3);
    expect(labels.find((label) => label.includes("Monday"))).toContain("Confirmed");
    const pendingLabel = labels.find((label) => label.includes("Tuesday"))!;
    expect(pendingLabel).toContain("Waiting for approval");
    expect(pendingLabel).not.toContain("Confirmed");
    const moveLabel = labels.find((label) => label.includes("Wednesday"))!;
    expect(moveLabel).toContain("Move request");
    expect(moveLabel).not.toContain("Confirmed");
    expect(host.querySelector(".cal-event.confirmed")).not.toBeNull();
    expect(host.querySelector(".cal-event.pending")).not.toBeNull();
    expect(host.querySelector(".cal-event.move")).not.toBeNull();
    // Times show in the business zone, not the machine's: 14:00 Auckland.
    expect(labels.find((label) => label.includes("Monday"))).toContain("2:00 PM");

    // Tapping a weekday heading opens Day for that date, with its open times.
    await press("Tuesday, Oct 13, open Day view");
    expect(host.querySelector("[aria-label='Available start times']")).not.toBeNull();
    expect(eventLabels()).toHaveLength(1);
    expect(availabilityCalls()).toBe(2);

    // Month and tapping a day: chips only, no open times, then Day again.
    await setView("month");
    expect(host.querySelector("[aria-label='Available start times']")).toBeNull();
    expect(host.querySelectorAll(".month-item")).toHaveLength(3);
    await press("Monday, Oct 12, open Day view");
    expect(eventLabels()[0]).toContain("Confirmed");
    expect(host.querySelector("[aria-label='Available start times']")).not.toBeNull();

    // Schedule lists the upcoming items; Year counts them; neither offers times.
    await setView("schedule");
    expect(host.querySelectorAll(".schedule-item")).toHaveLength(3);
    expect(host.textContent).toContain("Your move request");
    expect(host.querySelector("[aria-label='Available start times']")).toBeNull();
    await setView("year");
    expect(host.querySelector("[aria-label='Available start times']")).toBeNull();
    await press("Next year");
    expect(host.textContent).toContain("January 2027");

    // Only Day views asked for open times; the other views never did.
    expect(availabilityCalls()).toBe(3);
    expect(fetcher.mock.calls.every(([url]) => String(url).startsWith("https://api.example.test/v1/client/"))).toBe(true);
  });

  it("shows an empty calendar with a short note for past dates, and stops offering times past the horizon", async () => {
    await press("Previous day");
    expect(host.textContent).toContain("Past visits are not shown.");
    expect(host.querySelector("[aria-label='Available start times']")).toBeNull();
    expect(eventLabels()).toHaveLength(0);
    expect(availabilityCalls()).toBe(1);

    await setView("month");
    await press("Next month");
    await press("Next month");
    // Past the 14-day horizon the Day view asks for no times and says how far ahead requests go.
    await press("Today");
    await setView("day");
    for (let step = 0; step < 15; step += 1) await press("Next day");
    expect(host.textContent).toContain("Times can be requested up to 14 days ahead.");
    const asked = fetcher.mock.calls.map(([url]) => String(url));
    expect(asked.some((url) => url.endsWith("day=2026-10-24"))).toBe(true);
    expect(asked.some((url) => url.endsWith("day=2026-10-25"))).toBe(false);
  });
});
