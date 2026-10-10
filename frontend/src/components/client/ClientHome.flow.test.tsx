// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { useState } from "react";

import { useCalendarState } from "@/calendar/useCalendarState";
import { ClientHome } from "@/components/client/ClientHome";
import type { ClientBooking } from "@/lib/clientBookings";
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
    hold_expires_at: hold, requested_at: null, version: 1, replaces_appointment_id: null };
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

const ended = vi.fn();
/** Both client pages over one shell state, as the real shell shares them; `nav-*` buttons stand in for the menu. */
function Shell({ start }: { start: "calendar" | "appointments" }) {
  const [page, setPage] = useState(start);
  const calendar = useCalendarState({ ready: true, zone: ZONE, maxDate: null });
  const [moving, setMoving] = useState<ClientBooking | null>(null);
  const move = { booking: moving, set: setMoving };
  return <>
    <button type="button" data-nav="calendar" onClick={() => setPage("calendar")}>nav calendar</button>
    <button type="button" data-view="week" onClick={() => calendar.setView("week")}>week</button>
    <button type="button" data-nav="appointments" onClick={() => setPage("appointments")}>nav appointments</button>
    {page === "calendar"
      ? <ClientHome key="calendar" config={config} token="access" zone={ZONE} calendar={calendar} move={move} onSessionEnded={ended} />
      : <ClientHome key="appointments" config={config} token="access" zone={ZONE} move={move} onSessionEnded={ended}
        onOpenCalendar={() => { calendar.setView("day"); calendar.goToday(); setPage("calendar"); }} />}
  </>;
}

describe("client calendar and bookings", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.append(host); root = createRoot(host); ended.mockReset();
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-10-10T00:00:00Z") });
  });
  afterEach(async () => { vi.useRealTimers(); await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });
  const render = (start: "calendar" | "appointments" = "appointments") => act(async () => root.render(<Shell start={start} />));
  // The calendar view has its own status notes; the request outcome is the last one on the page.
  const note = () => Array.from(host.querySelectorAll("[aria-labelledby=client-times] [role=status]")).at(-1) ?? null;
  const nav = (page: string) => act(async () => host.querySelector<HTMLButtonElement>(`[data-nav=${page}]`)!.click());

  it("lists open starts in the business zone in Day view, without why other times are unavailable", async () => {
    const fetcher = vi.fn(async (url: string) => url.includes("availability")
      ? json({ day: TODAY, duration_minutes: 90, starts_at: [START] }) : json({ bookings: [] }));
    vi.stubGlobal("fetch", fetcher);
    await render("calendar");

    expect(host.textContent).toContain("asks the owner for approval");
    expect(host.textContent).toContain("Open times");
    expect(host.querySelector("[aria-label='Available start times'] button")?.textContent).toBe("10:00 AM");
    expect(host.textContent).toContain(ZONE);
    expect(host.textContent).toContain("about 90 minutes");
    const urls = fetcher.mock.calls.map(([url]) => url);
    expect(urls).toContain("https://api.example.test/v1/client/availability?day=2026-10-10");
    expect(urls.every((url) => url.startsWith("https://api.example.test/v1/client/"))).toBe(true);
    expect(urls.some((url) => url.includes("duration_minutes"))).toBe(false);
    expect(host.querySelector("select")).toBeNull();

    await act(async () => host.querySelector<HTMLButtonElement>("[aria-label='Available start times'] button")!.click());
    expect(host.textContent).toContain("2026-10-10 · 10:00 AM");
    expect(host.textContent).toContain("not confirmed until the owner approves");
    // Picking a time sends nothing; only the explicit send button does.
    expect(fetcher.mock.calls.every((call) => (call as unknown[])[1] === undefined
      || ((call as unknown[])[1] as RequestInit).method === undefined)).toBe(true);
  });

  it("moves a pending request to confirmed on refresh and keeps open times off the Appointments page", async () => {
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
    expect(host.textContent).toContain("Waiting for approval");
    expect(host.textContent).toContain("Not confirmed yet");
    expect(host.textContent).not.toContain("Confirmed");
    expect(host.querySelector("[aria-label='Available start times']")).toBeNull();
    expect(fetcher.mock.calls.some(([url]) => url.includes("availability"))).toBe(false);

    const refresh = () => Array.from(host.querySelectorAll("button")).find((b) => b.textContent === "Refresh")!;
    await act(async () => refresh().click());
    expect(host.textContent).toContain("The business has confirmed this visit.");
    expect(host.textContent).not.toContain("Waiting for approval");
    await act(async () => refresh().click());
    expect(host.textContent).toContain("no upcoming appointments");
  });

  describe("requesting a time", () => {
    const pending = { appointment_id: "hold-1", status: "PENDING_APPROVAL", start_at: START,
      end_at: "2026-10-09T22:30:00Z", duration_minutes: 90, hold_expires_at: "2026-10-11T00:00:00Z",
      requested_at: "2026-10-10T00:00:00Z" };
    const posts = (fetcher: ReturnType<typeof vi.fn>) => fetcher.mock.calls
      .filter((call) => (call[1] as RequestInit | undefined)?.method === "POST") as [string, RequestInit][];
    const pick = async () => {
      await act(async () => host.querySelector<HTMLButtonElement>("[aria-label='Available start times'] button")!.click());
    };
    const send = () => Array.from(host.querySelectorAll("button"))
      .find((b) => b.textContent?.includes("Send request for owner approval"))!;

    it("sends only the start with an idempotency key and shows pending, never confirmed", async () => {
      let sent = false;
      const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") { sent = true; return json(pending); }
        return url.includes("availability") ? json({ day: TODAY, duration_minutes: 90, starts_at: [START] })
          : json({ bookings: sent ? [pending] : [] });
      });
      vi.stubGlobal("fetch", fetcher);
      await render("calendar"); await pick();
      await act(async () => send().click());

      const [url, init] = posts(fetcher)[0]!;
      expect(url).toBe("https://api.example.test/v1/client/requests");
      expect(JSON.parse(init.body as string)).toEqual({ start_at: START });
      const headers = init.headers as Record<string, string>;
      expect(headers["Idempotency-Key"]).toBeTruthy();
      expect(headers.Authorization).toBe("Bearer access");
      expect(note()?.textContent).toContain("2026-10-10 · 10:00 AM");
      expect(note()?.textContent).toContain("waiting for owner approval");
      expect(host.querySelector("[aria-labelledby=client-times]")!.textContent).not.toContain("Confirmed");
      expect(host.querySelector("[aria-label='Request this time']")).toBeNull();
    });

    it("does not call a replayed request waiting when it was already declined", async () => {
      const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") return json({ ...pending, status: "DECLINED" });
        return url.includes("availability") ? json({ starts_at: [START] }) : json({ bookings: [] });
      });
      vi.stubGlobal("fetch", fetcher);
      await render("calendar"); await pick();
      await act(async () => send().click());
      const text = note()?.textContent ?? "";
      expect(text).toContain("no longer waiting for approval");
      expect(text).toContain("Declined");
      expect(text).not.toContain("Request sent");
    });

    it("reuses the key when a lost response is retried", async () => {
      let calls = 0;
      const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") { calls += 1; if (calls === 1) throw new Error("offline"); return json(pending); }
        return url.includes("availability") ? json({ starts_at: [START] }) : json({ bookings: [] });
      });
      vi.stubGlobal("fetch", fetcher);
      await render("calendar"); await pick();
      await act(async () => send().click());
      expect(host.querySelector("[role=alert]")?.textContent).toContain("will not create a second request");
      await act(async () => send().click());
      const keys = posts(fetcher).map(([, init]) => (init.headers as Record<string, string>)["Idempotency-Key"]);
      expect(keys).toHaveLength(2);
      expect(keys[0]).toBe(keys[1]);
      expect(note()?.textContent).toContain("waiting for owner approval");
    });

    it("offers current alternatives after a stale choice and sends nothing else", async () => {
      const alternative = "2026-10-09T22:00:00Z";
      const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") return json({ detail: { code: "SLOT_CONFLICT", message: "x", alternatives: [alternative] } }, 409);
        return url.includes("availability") ? json({ starts_at: [START] }) : json({ bookings: [] });
      });
      vi.stubGlobal("fetch", fetcher);
      await render("calendar"); await pick();
      await act(async () => send().click());
      const alert = host.querySelector("[role=alert]")!;
      expect(alert.textContent).toContain("nothing was requested");
      expect(alert.querySelector("button")?.textContent).toBe("2026-10-10 · 11:00 AM");
      expect(host.querySelector("[aria-labelledby=client-times]")!.textContent).not.toContain("Waiting for approval");
      expect(posts(fetcher)).toHaveLength(1);
      await act(async () => alert.querySelector<HTMLButtonElement>("button")!.click());
      expect(host.textContent).toContain("2026-10-10 · 11:00 AM");
      expect(host.querySelector("[aria-label='Request this time']")).not.toBeNull();
    });

    it.each([[403, null], [503, "not available right now"], [500, "was not sent"]])(
      "handles request status %i", async (status, message) => {
        vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
          if (init?.method === "POST") return json({}, status);
          return url.includes("availability") ? json({ starts_at: [START] }) : json({ bookings: [] });
        }));
        await render("calendar"); await pick();
        await act(async () => send().click());
        if (message === null) expect(ended).toHaveBeenCalled();
        else expect(host.querySelector("[role=alert]")?.textContent).toContain(message);
        expect(note()).toBeNull();
      });
  });

  it("requests the day the calendar is on", async () => {
    const fetcher = vi.fn(async (url: string) => url.includes("availability")
      ? json({ starts_at: [] }) : json({ bookings: [] }));
    vi.stubGlobal("fetch", fetcher);
    await render("calendar");
    expect(fetcher.mock.calls.map(([url]) => url)).toContain(
      "https://api.example.test/v1/client/availability?day=2026-10-10");
  });

  it.each([[403, null], [503, "not available right now"], [422, "Something went wrong"]])(
    "handles availability status %i", async (status, message) => {
      vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("availability")
        ? json({}, status) : json({ bookings: [] })));
      await render("calendar");
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
    expect(host.textContent).toContain("Checking status");
    expect(host.textContent).not.toContain("Expired");
    expect(host.textContent).toContain("Status unavailable");
    expect(host.textContent).toContain("Declined");
    expect(host.textContent).toContain("Cancelled");
    expect(host.textContent).not.toContain("Waiting for approval");
    expect(host.textContent).not.toContain("has confirmed");
  });

  it("ends the session when credentials are rejected", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({}, 401)));
    await render();
    expect(ended).toHaveBeenCalled();
  });

  it("retries a limited number of times while the server still says pending, then shows Expired only if told", async () => {
    vi.useRealTimers();
    vi.useFakeTimers({ now: new Date("2026-10-10T00:00:00Z") });
    const stale = { bookings: [booking("a", "PENDING_APPROVAL", 30, new Date(Date.now() - 60_000).toISOString())] };
    const fetcher = vi.fn(async (url: string) => url.includes("availability") ? json({ starts_at: [] }) : json(stale));
    vi.stubGlobal("fetch", fetcher);
    const bookingReads = () => fetcher.mock.calls.filter(([url]) => url.includes("bookings")).length;
    await render();
    expect(bookingReads()).toBe(1);
    for (let step = 0; step < 6; step += 1) await act(async () => { await vi.advanceTimersByTimeAsync(40_000); });
    expect(bookingReads()).toBe(5); // initial read plus four retries, then it stops
    expect(host.textContent).toContain("Checking status");
    expect(host.textContent).not.toContain("Expired");
  });

  it("classifies states without a pending hold becoming confirmed", () => {
    const pending = booking("a", "PENDING_APPROVAL", 1, new Date(Date.now() + 60_000).toISOString());
    expect(bookingState(pending, Date.now()).tone).toBe("pending");
    expect(bookingState(pending, Date.now() + 120_000).label).toBe("Checking status…");
    expect(bookingState(pending, Date.now() + 120_000).tone).toBe("pending");
    expect(bookingState({ ...pending, status: "EXPIRED" }, 0).tone).toBe("ended");
  });

  describe("cancelling and moving an appointment", () => {
    const original = { appointment_id: "visit-1", status: "CONFIRMED", start_at: "2026-10-12T21:00:00Z",
      end_at: "2026-10-12T22:30:00Z", duration_minutes: 90, hold_expires_at: null, requested_at: null,
      version: 3, replaces_appointment_id: null };
    const replacement = { appointment_id: "hold-2", status: "PENDING_APPROVAL", start_at: START,
      end_at: "2026-10-09T22:30:00Z", duration_minutes: 90, hold_expires_at: "2026-10-11T00:00:00Z",
      requested_at: "2026-10-10T00:00:00Z", version: 1, replaces_appointment_id: "visit-1" };
    const posts = (fetcher: ReturnType<typeof vi.fn>) => fetcher.mock.calls
      .filter((call) => (call[1] as RequestInit | undefined)?.method === "POST") as [string, RequestInit][];
    const button = (text: string) => Array.from(host.querySelectorAll("button"))
      .find((b) => b.textContent?.includes(text));
    const stub = (reads: unknown[], onPost: (url: string) => Response) => {
      const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") return onPost(url);
        if (url.includes("availability")) return json({ day: TODAY, duration_minutes: 90, starts_at: [START] });
        return json({ bookings: reads.length > 1 ? reads.shift() : reads[0] });
      });
      vi.stubGlobal("fetch", fetcher);
      return fetcher;
    };

    it("shows the exact appointment and sends nothing until the separate confirm press", async () => {
      const fetcher = stub([[original], []], () => json({ ...original, status: "CANCELLED", version: 4 }));
      await render();
      expect(host.textContent).toContain("Confirmed");
      await act(async () => button("Cancel appointment")!.click());
      const confirm = host.querySelector("[aria-label='Confirm cancellation']")!;
      expect(confirm.textContent).toContain("2026-10-13 · 10:00 AM");
      expect(confirm.textContent).toContain(ZONE);
      expect(posts(fetcher)).toHaveLength(0);
      await act(async () => button("No, keep it")!.click());
      expect(host.querySelector("[aria-label='Confirm cancellation']")).toBeNull();
      expect(posts(fetcher)).toHaveLength(0);

      await act(async () => button("Cancel appointment")!.click());
      await act(async () => button("Yes, cancel this appointment")!.click());
      const [url, init] = posts(fetcher)[0]!;
      expect(url).toBe("https://api.example.test/v1/client/bookings/visit-1/cancel");
      expect(JSON.parse(init.body as string)).toEqual({ expected_version: 3 });
      expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
      expect(host.querySelector("[role=status]")?.textContent).toContain("Cancelled");
      expect(host.querySelector("[role=status]")?.textContent).toContain("owner was told");
      expect(host.textContent).toContain("no upcoming appointments");
    });

    it("reuses the key after a lost response and refuses a stale appointment without cancelling", async () => {
      let attempts = 0;
      const fetcher = stub([[original]], () => {
        attempts += 1;
        if (attempts === 1) throw new Error("offline");
        return json({ detail: { code: "STALE_BOOKING", message: "x", alternatives: [] } }, 409);
      });
      await render();
      await act(async () => button("Cancel appointment")!.click());
      await act(async () => button("Yes, cancel this appointment")!.click());
      expect(host.querySelector("[role=alert]")?.textContent).toContain("Could not tell whether this was sent");
      await act(async () => button("Yes, cancel this appointment")!.click());
      const keys = posts(fetcher).map(([, init]) => (init.headers as Record<string, string>)["Idempotency-Key"]);
      expect(keys).toHaveLength(2);
      expect(keys[0]).toBe(keys[1]);
      expect(host.querySelector("[role=alert]")?.textContent).toContain("changed since you last looked");
      expect(host.querySelector("[role=status]")).toBeNull();
      expect(host.textContent).toContain("Confirmed");
    });

    it("moves with a pending replacement and never presents it as confirmed", async () => {
      const fetcher = stub([[original], [original], [original, replacement]], () => json(replacement));
      await render();
      await act(async () => button("Move to another time")!.click()); // opens the Calendar's Day view
      expect(host.querySelector("[aria-label='Moving this appointment']")?.textContent)
        .toContain("stays confirmed until the owner approves");
      await act(async () => host.querySelector<HTMLButtonElement>("[aria-label='Available start times'] button")!.click());
      await act(async () => button("Send move request for owner approval")!.click());

      const [url, init] = posts(fetcher)[0]!;
      expect(url).toBe("https://api.example.test/v1/client/bookings/visit-1/reschedule");
      expect(JSON.parse(init.body as string)).toEqual({ start_at: START, expected_version: 3 });
      const status = note()?.textContent ?? "";
      expect(status).toContain("Move requested");
      expect(status).toContain("not confirmed yet");
      expect(status).toContain("original appointment is still confirmed");
      await nav("appointments");
      const items = Array.from(host.querySelectorAll("li.card"));
      expect(items).toHaveLength(2);
      expect(items[0]!.textContent).toContain("Confirmed");
      expect(items[0]!.textContent).toContain("waiting for the owner");
      expect(items[0]!.textContent).not.toContain("Move to another time");
      expect(items[0]!.textContent).not.toContain("Cancel appointment");
      expect(items[1]!.textContent).toContain("Move request: waiting for approval");
      expect(items[1]!.textContent).toContain("Not confirmed");
      expect(items[1]!.textContent).toContain("stays confirmed until the owner approves");
      expect(items[1]!.textContent).not.toMatch(/\bConfirmed\b/);
      expect(items[1]!.textContent).toContain("Withdraw move request");
    });

    it("withdraws only the replacement, leaving the original", async () => {
      const fetcher = stub([[original, replacement], [original]],
        () => json({ ...replacement, status: "CANCELLED", version: 2 }));
      await render();
      await act(async () => button("Withdraw move request")!.click());
      expect(host.querySelector("[aria-label='Confirm cancellation']")?.textContent)
        .toContain("original appointment stays confirmed");
      await act(async () => button("Yes, withdraw this request")!.click());
      expect(posts(fetcher)[0]![0]).toBe("https://api.example.test/v1/client/bookings/hold-2/cancel");
      expect(host.textContent).toContain("Confirmed");
      expect(host.querySelectorAll("li.card")).toHaveLength(1);
      expect(button("Move to another time")).toBeTruthy(); // declined, expired, or withdrawn: original intact
    });

    it("shows the open times and creates nothing when the new time was taken", async () => {
      stub([[original]], () => json({ detail: { code: "SLOT_CONFLICT", message: "x",
        alternatives: ["2026-10-09T23:00:00Z"] } }, 409));
      await render();
      await act(async () => button("Move to another time")!.click());
      await act(async () => host.querySelector<HTMLButtonElement>("[aria-label='Available start times'] button")!.click());
      await act(async () => button("Send move request for owner approval")!.click());
      expect(host.querySelector("[role=alert]")?.textContent).toContain("no longer open");
      expect(host.querySelector("[aria-label='Other open times']")).not.toBeNull();
      expect(host.textContent).not.toContain("Move requested");
    });

    describe("a move held while bookings are still loading", () => {
      const slow = (second: unknown) => {
        let release: (response: Response) => void = () => undefined;
        let bookingReads = 0;
        const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
          if (init?.method === "POST") return json(replacement);
          if (url.includes("availability")) return json({ day: TODAY, duration_minutes: 90, starts_at: [START] });
          bookingReads += 1;
          if (bookingReads === 1) return json({ bookings: [original] });
          return new Promise<Response>((resolve) => { release = resolve; });
        });
        vi.stubGlobal("fetch", fetcher);
        return { fetcher, finish: () => act(async () => release(json({ bookings: second }))) };
      };
      const timeButtons = () => host.querySelectorAll("[aria-label='Available start times'] button");

      it("offers no time and posts no new request until the booking is known", async () => {
        const { fetcher, finish } = slow([original]);
        await render();
        await act(async () => button("Move to another time")!.click());
        // Availability has resolved; the booking has not.
        expect(timeButtons()).toHaveLength(0);
        expect(host.textContent).toContain("Loading times");
        await finish();
        expect(timeButtons()).toHaveLength(1);
        expect(host.querySelector("[aria-label='Moving this appointment']")).not.toBeNull();
        await act(async () => (timeButtons()[0] as HTMLElement).click());
        expect(button("Send request for owner approval")).toBeUndefined();
        await act(async () => button("Send move request for owner approval")!.click());
        expect(posts(fetcher).map(([url]) => url)).toEqual(["https://api.example.test/v1/client/bookings/visit-1/reschedule"]);
      });

      it("drops a move whose booking changed, says so, and sends nothing", async () => {
        const { fetcher, finish } = slow([{ ...original, version: 4 }]);
        await render();
        await act(async () => button("Move to another time")!.click());
        await finish();
        expect(host.querySelector("[aria-label='Moving this appointment']")).toBeNull();
        expect(host.querySelector("[role=alert]")?.textContent).toContain("move was cancelled and nothing was sent");
        expect(posts(fetcher)).toHaveLength(0);
      });

      it("shows the moving notice outside Day view and on Appointments, each with a way to keep the appointment", async () => {
        stub([[original]], () => json({}));
        await render();
        await act(async () => button("Move to another time")!.click());
        await act(async () => host.querySelector<HTMLButtonElement>("[data-view=week]")!.click());
        expect(host.textContent).toContain("You are moving your appointment");
        await nav("appointments");
        expect(host.textContent).toContain("You started moving your appointment");
        await act(async () => button("Keep my current appointment")!.click());
        expect(host.textContent).not.toContain("started moving");
        await nav("calendar");
        expect(host.textContent).not.toContain("You are moving");
      });
    });

    it("offers no change without a version from the server", async () => {
      stub([[{ ...original, version: undefined }]], () => json({}));
      await render();
      expect(button("Cancel appointment")).toBeUndefined();
      expect(button("Move to another time")).toBeUndefined();
    });

    it("uses cancel-appointment wording for a confirmed, already moved appointment", async () => {
      stub([[{ ...replacement, status: "CONFIRMED", version: 2 }]], () => json({}));
      await render();
      expect(button("Withdraw")).toBeUndefined();
      await act(async () => button("Cancel appointment")!.click());
      const text = host.querySelector("[aria-label='Confirm cancellation']")!.textContent ?? "";
      expect(text).toContain("Cancel this confirmed appointment?");
      expect(button("Yes, cancel this appointment")).toBeTruthy();
      expect(text).not.toContain("withdraw");
    });

    it("drops the cancel panel after a stale refusal and after Refresh, never resending the old version", async () => {
      const fetcher = stub([[original]], () => json({ detail: { code: "STALE_BOOKING", message: "x", alternatives: [] } }, 409));
      await render();
      await act(async () => button("Cancel appointment")!.click());
      await act(async () => button("Yes, cancel this appointment")!.click());
      expect(host.querySelector("[aria-label='Confirm cancellation']")).toBeNull();
      expect(host.querySelector("[role=alert]")?.textContent).toContain("changed since you last looked");
      await act(async () => button("Cancel appointment")!.click());
      await act(async () => button("Refresh")!.click());
      expect(host.querySelector("[aria-label='Confirm cancellation']")).toBeNull();
      expect(posts(fetcher)).toHaveLength(1);
    });
  });
});
