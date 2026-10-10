// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ClientHome } from "@/components/client/ClientHome";
import { bookingState } from "@/lib/clientBookings";
import { parseConfig } from "@/lib/config";
import { todayKey } from "@/lib/time";

const config = parseConfig({ authMode: "cognito", apiBaseUrl: "https://api.example.test",
  businessId: "pilot", cognitoDomain: "https://auth.example.test", clientId: "public" });
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;

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
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });
  const render = () => act(async () => root.render(<ClientHome config={config} token="access" onSessionEnded={ended} />));

  it("lists starts and own bookings, moving a pending request to confirmed on refresh", async () => {
    const today = todayKey(zone);
    const start = new Date(); start.setHours(23, 0, 0, 0);
    const bookingReads = [
      { bookings: [booking("a", "PENDING_APPROVAL", 30, new Date(Date.now() + 3_600_000).toISOString())] },
      { bookings: [booking("a", "CONFIRMED", 30)] },
      { bookings: [] },
    ];
    const fetcher = vi.fn(async (url: string) => url.includes("availability")
      ? json({ day: today, duration_minutes: 120, starts_at: [start.toISOString()] })
      : json(bookingReads.shift() ?? { bookings: [] }));
    vi.stubGlobal("fetch", fetcher);
    await render();

    expect(host.textContent).toContain("asks the owner for approval");
    expect(host.querySelector("[aria-label='Available start times'] button")).not.toBeNull();
    expect(host.textContent).toContain("Waiting for approval");
    expect(host.textContent).toContain("Not confirmed yet");
    expect(host.textContent).not.toContain("Confirmed");
    expect(fetcher.mock.calls.every(([url]) => url.startsWith("https://api.example.test/v1/client/"))).toBe(true);

    const refresh = () => Array.from(host.querySelectorAll("button")).find((b) => b.textContent === "Refresh")!;
    await act(async () => refresh().click());
    expect(host.textContent).toContain("The business has confirmed this visit.");
    expect(host.textContent).not.toContain("Waiting for approval");
    await act(async () => refresh().click());
    expect(host.textContent).toContain("no upcoming appointments");

    await act(async () => host.querySelector<HTMLButtonElement>("[aria-label='Available start times'] button")!.click());
    expect(host.textContent).toContain("nothing has been sent or held");
    expect(fetcher.mock.calls.every((call) => (call as unknown[])[1] === undefined || ((call as unknown[])[1] as RequestInit).method === undefined)).toBe(true);
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
