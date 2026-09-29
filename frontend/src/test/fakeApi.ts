import type { CalendarSnapshot, ClientProfile, PolicyState } from "@/api/types";
import { OwnerApi } from "@/lib/api";
import { parseConfig } from "@/lib/config";

export const ZONE = "America/Los_Angeles";

export const POLICY: PolicyState = {
  calendar_revision: 4,
  record: {
    version: 2,
    policy: {
      timezone: ZONE, weekly_windows: { "0": [{ opens: "08:00:00", closes: "17:00:00" }] },
      booking_horizon_days: 14, slot_increment_minutes: 15, maximum_visit_minutes: 180,
      minimum_visit_gap_minutes: 30, opening_buffer_minutes: 0, closing_buffer_minutes: 0,
      hold_minutes: 1440, maximum_buffer_minutes: 30, date_exceptions: {}, holiday_calendar: "US_FEDERAL",
    },
  },
};

export const CALENDAR: CalendarSnapshot = { business_id: "pilot", revision: 4, events: [] };

export const CLIENT: ClientProfile = {
  business_id: "pilot", client_id: "client-1", name: "Avery Example", phone_e164: "+14155550101",
  service_address: "1 Example Way", home_size: "medium", default_duration_minutes: 120, active: true,
  version: 1, created_at: "2026-07-01T00:00:00Z", updated_at: "2026-07-01T00:00:00Z",
  phone_verified_at: null,
};

export type Route = (method: string, path: string, body: unknown) => { status: number; body: unknown } | undefined;

/** An OwnerApi backed by an in-test router; unknown routes fail loudly. */
export function fakeApi(route: Route, onUnauthorized = () => undefined) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const path = url.pathname.replace("/v1/owner/businesses/pilot", "") + url.search;
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body });
    const defaults: Record<string, unknown> = {
      "/calendar": CALENDAR, "/requests": [], "/clients": [CLIENT], "/policy": POLICY };
    const result = route(method, path, body)
      ?? (method === "GET" && path in defaults ? { status: 200, body: defaults[path] } : undefined);
    if (!result) throw new Error(`Unexpected ${method} ${path}`);
    return new Response(JSON.stringify(result.body), { status: result.status });
  });
  const config = parseConfig({ authMode: "local", apiBaseUrl: "http://127.0.0.1:8000", businessId: "pilot" });
  return { api: new OwnerApi(config, "tok", onUnauthorized, fetcher as typeof fetch), calls };
}
