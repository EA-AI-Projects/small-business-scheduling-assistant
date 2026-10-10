// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ClientHome } from "@/components/client/ClientHome";
import { bookingState } from "@/lib/clientBookings";
import { parseConfig } from "@/lib/config";

const config = parseConfig({ authMode: "cognito", apiBaseUrl: "https://api.example.test",
  businessId: "pilot", cognitoDomain: "https://auth.example.test", clientId: "public" });
// A zone unlike the Pacific business zone and unlikely to equal the test machine's zone.
const ZONE = "Pacific/Auckland";
const TODAY = "2026-10-10";
// 2026-10-10 10:00 in Auckland (UTC+13) is 2026-10-09 21:00 UTC.
const START = "2026-10-09T21:00:00Z";

function booking(id: string, status: string, hours: number, hold: string | null = null) {
  const start = new Date(Date.now() + hours * 3_600_000);
  return { appointment_id: id, status, start_at: start.toISOString(),
    end_at: new Date(start.getTime() + 7_200_000).toISOString(), duration_minutes: 120,
    hold_expires_at: hold, requested_at: null };
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

describe("client calendar and bookings", () => {
  let host: HTMLDivElement;
  let root: Root;
  const ended = vi.fn();
  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.append(host); root = createRoot(host); ended.mockReset();
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-10-10T00:00:00Z") });
  });
  afterEach(async () => { vi.useRealTimers(); await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });
  const render = () => act(async () => root.render(<ClientHome config={config} token="access" zone={ZONE} onSessionEnded={ended} />));

  it("lists starts in the business zone and moves a pending request to confirmed on refresh", async () => {
    const bookingReads = [
      { bookings: [booking("a", "PENDING_APPROVAL", 30, new Date(Date.now() + 3_600_000).toISOString())] },
      { bookings: [booking("a", "CONFIRMED", 30)] },
      { bookings: [] },
    ];
    const fetcher = vi.fn(async (url: string) => url.includes("availability")
      ? json({ day: TODAY, duration_minutes: 90, starts_at: [START] })
      : json(bookingReads.shift() ?? { bookings: [] }));
    vi.stubGlobal("fetch", fetcher);
    await render();

    expect(host.textContent).toContain("asks the owner for approval");
    expect(host.querySelector("[aria-label='Available start times'] button")?.textContent).toBe("10:00 AM");
    expect(host.textContent).toContain(ZONE);
    expect(host.textContent).toContain("about 90 minutes");
    expect(host.textContent).toContain("Waiting for approval");
    expect(host.textContent).toContain("Not confirmed yet");
    expect(host.textContent).not.toContain("Confirmed");
    const urls = fetcher.mock.calls.map(([url]) => url);
    expect(urls).toContain("https://api.example.test/v1/client/availability?day=2026-10-10");
    expect(urls.every((url) => url.startsWith("https://api.example.test/v1/client/"))).toBe(true);
    expect(urls.some((url) => url.includes("duration_minutes"))).toBe(false);
    expect(host.querySelector("select")).toBeNull();

    const refresh = () => Array.from(host.querySelectorAll("button")).find((b) => b.textContent === "Refresh")!;
    const availabilityCalls = () => fetcher.mock.calls.filter(([url]) => url.includes("availability")).length;
    const before = availabilityCalls();
    await act(async () => refresh().click());
    expect(availabilityCalls()).toBe(before + 1);
    expect(host.textContent).toContain("The business has confirmed this visit.");
    expect(host.textContent).not.toContain("Waiting for approval");
    await act(async () => refresh().click());
    expect(host.textContent).toContain("no upcoming appointments");

    await act(async () => host.querySelector<HTMLButtonElement>("[aria-label='Available start times'] button")!.click());
    expect(host.textContent).toContain("nothing has been sent or held");
    expect(host.textContent).toContain("2026-10-10 · 10:00 AM");
    expect(fetcher.mock.calls.every((call) => (call as unknown[])[1] === undefined
      || ((call as unknown[])[1] as RequestInit).method === undefined)).toBe(true);
  });

  it("requests the picked business day", async () => {
    const fetcher = vi.fn(async (url: string) => url.includes("availability")
      ? json({ starts_at: [] }) : json({ bookings: [] }));
    vi.stubGlobal("fetch", fetcher);
    await render();
    const day = host.querySelector<HTMLElement>("[data-date='2026-10-12']")!;
    await act(async () => day.click());
    expect(fetcher.mock.calls.map(([url]) => url)).toContain(
      "https://api.example.test/v1/client/availability?day=2026-10-12");
  });

  it.each([[403, null], [503, "not available right now"], [422, "Something went wrong"]])(
    "handles availability status %i", async (status, message) => {
      vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("availability")
        ? json({}, status) : json({ bookings: [] })));
      await render();
      if (message === null) expect(ended).toHaveBeenCalled();
      else {
        expect(ended).not.toHaveBeenCalled();
        expect(host.querySelector("[role=alert]")?.textContent).toContain(message);
      }
      expect(host.querySelector("[aria-label='Available start times']")).toBeNull();
    });

  it("shows no times without a business time zone", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<ClientHome config={config} token="access" zone={null} onSessionEnded={ended} />));
    expect(host.textContent).toContain("not available right now");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("re-reads bookings when a pending hold passes", async () => {
    const reads = vi.fn(() => json({ bookings: [] }));
    const first = { bookings: [booking("a", "PENDING_APPROVAL", 30, new Date(Date.now() + 100).toISOString())] };
    let count = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("availability") ? json({ starts_at: [] })
      : (count++ === 0 ? json(first) : reads())));
    await render();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 600)); });
    expect(reads).toHaveBeenCalled();
    expect(host.textContent).toContain("no upcoming appointments");
  });

  it("shows a lapsed hold as expired and unknown statuses without implying confirmation", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("availability") ? json({ starts_at: [] })
      : json({ bookings: [booking("p", "PENDING_APPROVAL", 5, new Date(Date.now() - 1000).toISOString()),
        booking("x", "SOMETHING_NEW", 6), booking("d", "DECLINED", 7), booking("c", "CANCELLED", 8)] })));
    await render();
    expect(host.textContent).toContain("Expired");
    expect(host.textContent).toContain("Status unavailable");
    expect(host.textContent).toContain("Declined");
    expect(host.textContent).toContain("Cancelled");
    expect(host.textContent).not.toContain("Waiting for approval");
    expect(host.textContent).not.toContain("has confirmed");
    expect(host.textContent).toContain("No times are available");
  });

  it("ends the session when credentials are rejected", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({}, 401)));
    await render();
    expect(ended).toHaveBeenCalled();
  });

  it("classifies states without a pending hold becoming confirmed", () => {
    const pending = booking("a", "PENDING_APPROVAL", 1, new Date(Date.now() + 60_000).toISOString());
    expect(bookingState(pending, Date.now()).tone).toBe("pending");
    expect(bookingState(pending, Date.now() + 120_000).label).toBe("Expired");
    expect(bookingState({ ...pending, status: "EXPIRED" }, 0).tone).toBe("ended");
  });
});
