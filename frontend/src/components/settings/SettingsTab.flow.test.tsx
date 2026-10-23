// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ApiError, type OwnerApi } from "@/lib/api";
import { OwnerProvider } from "@/owner/OwnerContext";

import { SettingsTab } from "./SettingsTab";

const PROFILE = { display_name: "Example Cleaning", description: "Home cleaning.",
  support_contact: "help@example.test", service_name: "Standard clean",
  service_description: "A two-hour clean.", default_duration_minutes: 120 };
const POLICY = { timezone: "America/Los_Angeles", booking_horizon_days: 14,
  weekly_windows: { "0": [{ opens: "08:00:00", closes: "17:00:00" }] },
  date_exceptions: {}, holiday_calendar: "US_FEDERAL", slot_increment_minutes: 15,
  maximum_visit_minutes: 180, maximum_buffer_minutes: 30, minimum_visit_gap_minutes: 30,
  opening_buffer_minutes: 0, closing_buffer_minutes: 0, hold_minutes: 1440 };
let profileState = { profile: PROFILE, version: 1 };
let policyState: { record: { policy: typeof POLICY; version: number }; calendar_revision: number } | null =
  { record: { policy: POLICY, version: 1 }, calendar_revision: 1 };
let outreachState = { version: 1, settings: { enabled: false, weekday: null as number | null,
  local_time: null as string | null, lookahead_weeks: null as number | null, message: null as string | null } };
const get = vi.fn(async (path: string) => {
  if (path === "/profile") return profileState;
  if (path === "/policy") return policyState;
  if (path === "/booking-outreach") return outreachState;
  if (path === "/scheduling-preferences") return { version: 0, active: null };
  if (path === "/calendar") return { revision: 1, events: [] };
  return [];
});
const request = vi.fn();
const api = { get, request } as unknown as OwnerApi;

function setValue(element: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("owner Settings", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(async () => {
    get.mockClear();
    request.mockReset();
    profileState = { profile: PROFILE, version: 1 };
    policyState = { record: { policy: POLICY, version: 1 }, calendar_revision: 1 };
    outreachState = { version: 1, settings: { enabled: false, weekday: null,
      local_time: null, lookahead_weeks: null, message: null } };
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><SettingsTab /></OwnerProvider>));
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });

  it("shows six saved-value cards and opens Business details with collapsible sections", async () => {
    const cards = [...host.querySelectorAll<HTMLButtonElement>('button[aria-haspopup="dialog"]')];
    expect(cards).toHaveLength(6);
    expect(cards.map((card) => card.textContent)).toEqual([
      expect.stringContaining("Business details"), expect.stringContaining("Availability"),
      expect.stringContaining("Advanced scheduling"), expect.stringContaining("Booking invitations"),
      expect.stringContaining("Scheduling preferences"), expect.stringContaining("Visit reminders"),
    ]);
    expect(cards[0]!.textContent).toContain("Example Cleaning");
    expect(cards[1]!.textContent).toContain("1 open day");
    expect(cards[1]!.textContent).toContain("Federal holidays closed");
    expect(host.textContent).not.toContain("Current policy");

    await act(async () => cards[0]!.click());
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
    expect(dialog).not.toBeNull();
    expect(dialog.textContent).toContain("Business name");
    const business = [...dialog.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')]
      .find((button) => button.textContent?.includes("Business"))!;
    const service = [...dialog.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')]
      .find((button) => button.textContent?.includes("Service"))!;
    expect(business.getAttribute("aria-expanded")).toBe("true");
    expect(service.getAttribute("aria-expanded")).toBe("false");
    await act(async () => service.click());
    expect(service.getAttribute("aria-expanded")).toBe("true");
  });

  it("keeps a collapsed Business draft and asks before discarding it", async () => {
    const opener = host.querySelector<HTMLButtonElement>('button[aria-label="Edit Business details"]')!;
    await act(async () => opener.click());
    const field = document.querySelector<HTMLInputElement>('#business-profile-display_name')!;
    await act(async () => setValue(field, "Renamed Cleaning"));
    expect(opener.textContent).toContain("Example Cleaning");
    expect(opener.textContent).toContain("Unsaved changes");

    const section = [...document.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')]
      .find((button) => button.textContent?.includes("Business"))!;
    await act(async () => section.click());
    await act(async () => section.click());
    expect(field.value).toBe("Renamed Cleaning");

    await act(async () => document.querySelector<HTMLButtonElement>('.popover-close')!.click());
    const confirm = document.querySelector<HTMLElement>('[role="alertdialog"]')!;
    expect(confirm).not.toBeNull();
    expect(document.activeElement?.textContent).toBe("Keep editing");
    await act(async () => [...confirm.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Keep editing")!.click());
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(field.value).toBe("Renamed Cleaning");
    expect(document.querySelector('[role="dialog"]')?.contains(document.activeElement)).toBe(true);

    await act(async () => document.querySelector<HTMLButtonElement>('.popover-close')!.click());
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>('[role="alertdialog"] button')]
      .find((button) => button.textContent === "Discard changes")!.click());
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(opener.textContent).not.toContain("Unsaved changes");
    await act(async () => opener.click());
    expect(document.querySelector<HTMLInputElement>('#business-profile-display_name')!.value)
      .toBe("Example Cleaning");
  });

  it("shows a saved Business name on its card only after Save succeeds", async () => {
    const opener = host.querySelector<HTMLButtonElement>('button[aria-label="Edit Business details"]')!;
    await act(async () => opener.click());
    await act(async () => setValue(document.querySelector<HTMLInputElement>('#business-profile-display_name')!,
      "Renamed Cleaning"));
    expect(opener.textContent).toContain("Example Cleaning");
    request.mockImplementation(async (path: string, options: { body: { profile: typeof PROFILE } }) => {
      expect(path).toBe("/profile");
      profileState = { profile: options.body.profile, version: 2 };
      return profileState;
    });
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save business details")!.click());
    expect(opener.textContent).toContain("Renamed Cleaning");
    expect(opener.textContent).not.toContain("Unsaved changes");
    await act(async () => document.querySelector<HTMLButtonElement>('.popover-close')!.click());
    expect(document.querySelector('[role="alertdialog"]')).toBeNull();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("keeps a Business draft after a stale save while its card shows saved values", async () => {
    const opener = host.querySelector<HTMLButtonElement>('button[aria-label="Edit Business details"]')!;
    await act(async () => opener.click());
    const field = document.querySelector<HTMLInputElement>('#business-profile-display_name')!;
    await act(async () => setValue(field, "Renamed Cleaning"));
    request.mockRejectedValueOnce(new ApiError("stale", 409, "STALE_VERSION",
      { profile: { ...PROFILE, service_name: "New service" }, version: 2 }));
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save business details")!.click());
    expect(field.value).toBe("Renamed Cleaning");
    expect(opener.textContent).toContain("Example Cleaning");
    expect(opener.textContent).toContain("Unsaved changes");
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain("changed elsewhere");
  });

  it("opens a collapsed Service section and focuses its invalid field", async () => {
    await act(async () => host.querySelector<HTMLButtonElement>('button[aria-label="Edit Business details"]')!.click());
    const service = [...document.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')]
      .find((button) => button.textContent?.includes("Service"))!;
    await act(async () => service.click());
    const field = document.querySelector<HTMLInputElement>('#business-profile-service_name')!;
    await act(async () => setValue(field, ""));
    await act(async () => service.click());
    expect(service.getAttribute("aria-expanded")).toBe("false");
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save business details")!.click());
    expect(request).not.toHaveBeenCalled();
    expect(service.getAttribute("aria-expanded")).toBe("true");
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(field);
  });

  it("returns keyboard focus after Escape and uses the phone sheet when narrow", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
    const opener = host.querySelector<HTMLButtonElement>('button[aria-label="Edit Business details"]')!;
    opener.focus();
    await act(async () => opener.click());
    expect(document.querySelector('[role="dialog"]')?.classList.contains("sheet")).toBe(true);
    expect(document.activeElement?.getAttribute("role")).toBe("dialog");
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1)); });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("keeps saved policy and invitation details reachable from their cards", async () => {
    const cases = [
      { label: "Edit Availability", expected: "08:00–17:00" },
      { label: "Edit Advanced scheduling", expected: "Business timezone" },
      { label: "Edit Booking invitations", expected: "Enable invitations" },
    ];
    for (const item of cases) {
      await act(async () => host.querySelector<HTMLButtonElement>(`button[aria-label="${item.label}"]`)!.click());
      expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1);
      expect(document.querySelector('[role="dialog"]')?.textContent).toContain(item.expected);
      await act(async () => document.querySelector<HTMLButtonElement>('.popover-close')!.click());
    }
  });

  it("previews a split week without changing the saved card until regular hours save", async () => {
    const opener = host.querySelector<HTMLButtonElement>('[aria-label="Edit Availability"]')!;
    await act(async () => opener.click());
    const days = [...document.querySelectorAll<HTMLButtonElement>(".weekly-day")];
    expect(days).toHaveLength(7);
    expect(days[0]!.textContent).toContain("08:00–17:00");
    expect(days[1]!.textContent).toContain("Closed");
    await act(async () => days[1]!.click());
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Add Tuesday hours"]')!.click());
    expect(days[1]!.textContent).toContain("08:00–17:00");
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Add Tuesday hours"]')!.click());
    const closes = [...document.querySelectorAll<HTMLInputElement>('input[type="time"]')]
      .filter((field) => field.closest('[aria-label="Tuesday hours"]'));
    await act(async () => { setValue(closes[0]!, "08:00"); setValue(closes[1]!, "12:00");
      setValue(closes[2]!, "13:00"); setValue(closes[3]!, "17:00"); });
    expect(days[1]!.textContent).toContain("08:00–12:00, 13:00–17:00");
    expect(opener.textContent).toContain("1 open day");
    expect(opener.textContent).toContain("Unsaved changes");
    request.mockImplementationOnce(async (_path: string, options: { body: { policy: typeof POLICY } }) => {
      policyState = { record: { policy: options.body.policy, version: 2 }, calendar_revision: 2 };
      return policyState;
    });
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save regular settings")!.click());
    expect(opener.textContent).toContain("2 open days");
    expect(opener.textContent).not.toContain("Unsaved changes");
  });

  it("reveals and focuses a collapsed invalid advanced field", async () => {
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Edit Advanced scheduling"]')!.click());
    const timezoneSection = [...document.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')]
      .find((button) => button.textContent?.includes("Business timezone"))!;
    expect(timezoneSection.getAttribute("aria-expanded")).toBe("false");
    await act(async () => timezoneSection.click());
    const field = document.querySelector<HTMLInputElement>('#advanced-policy-timezone')!;
    await act(async () => setValue(field, "Invalid/Zone"));
    await act(async () => timezoneSection.click());
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save advanced settings")!.click());
    expect(request).not.toHaveBeenCalled();
    expect(timezoneSection.getAttribute("aria-expanded")).toBe("true");
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(field);
  });

  it("selects a saved date exception and restores normal hours after a failed retry", async () => {
    policyState = { record: { policy: { ...POLICY, date_exceptions: { "2026-12-25": [] } }, version: 1 },
      calendar_revision: 1 };
    await act(async () => root.unmount());
    root = createRoot(host);
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><SettingsTab /></OwnerProvider>));
    const opener = host.querySelector<HTMLButtonElement>('[aria-label="Edit Availability"]')!;
    expect(opener.textContent).toContain("1 date exception");
    await act(async () => opener.click());
    const exceptions = [...document.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')]
      .find((button) => button.textContent?.includes("Date exceptions"))!;
    await act(async () => exceptions.click());
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>(".exception-list button")][0]!.click());
    expect(document.querySelector<HTMLInputElement>('input[name="date"]')!.value).toBe("2026-12-25");
    const mode = document.querySelector<HTMLSelectElement>('select[name="mode"]')!;
    await act(async () => { mode.value = "normal"; mode.dispatchEvent(new Event("change", { bubbles: true })); });
    request.mockRejectedValueOnce(new ApiError("conflict", 409, "SLOT_CONFLICT", null));
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save exception")!.click());
    expect(opener.textContent).toContain("1 date exception");
    expect(opener.textContent).toContain("Unsaved changes");
    expect(document.querySelector<HTMLInputElement>('input[name="date"]')!.value).toBe("2026-12-25");
    request.mockImplementationOnce(async (_path: string, options: { body: { policy: typeof POLICY } }) => {
      policyState = { record: { policy: options.body.policy, version: 2 }, calendar_revision: 2 };
      return policyState;
    });
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save exception")!.click());
    expect(opener.textContent).toContain("0 date exceptions");
  });

  it("preserves every period when editing a split-hours date exception", async () => {
    policyState = { record: { policy: { ...POLICY, date_exceptions: { "2026-12-25": [
      { opens: "08:00:00", closes: "12:00:00" }, { opens: "13:00:00", closes: "17:00:00" },
    ] } }, version: 1 }, calendar_revision: 1 };
    await act(async () => root.unmount());
    root = createRoot(host);
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><SettingsTab /></OwnerProvider>));
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Edit Availability"]')!.click());
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')]
      .find((button) => button.textContent?.includes("Date exceptions"))!.click());
    await act(async () => document.querySelector<HTMLButtonElement>(".exception-list button")!.click());
    expect(document.querySelectorAll('form[aria-label="Date exception"] input[type="time"]')).toHaveLength(4);
    const opener = host.querySelector<HTMLButtonElement>('[aria-label="Edit Availability"]')!;
    await act(async () => setValue(document.querySelector<HTMLInputElement>('input[name="closes"]')!, "11:00"));
    await act(async () => setValue(document.querySelector<HTMLInputElement>('input[name="closes"]')!, "12:00"));
    expect(opener.textContent).not.toContain("Unsaved changes");
    await act(async () => setValue(document.querySelector<HTMLInputElement>('input[name="closes"]')!, "11:00"));
    request.mockImplementationOnce(async (_path: string, options: { body: { policy: typeof POLICY & {
      date_exceptions: Record<string, { opens: string; closes: string }[]> } } }) => {
      expect(options.body.policy.date_exceptions["2026-12-25"]).toEqual([
        { opens: "08:00", closes: "11:00" }, { opens: "13:00", closes: "17:00" },
      ]);
      policyState = { record: { policy: options.body.policy, version: 2 }, calendar_revision: 2 };
      return policyState;
    });
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save exception")!.click());
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("loads a pilot policy from Availability when no policy exists", async () => {
    policyState = null;
    await act(async () => root.unmount());
    root = createRoot(host);
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><SettingsTab /></OwnerProvider>));
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Edit Availability"]')!.click());
    request.mockImplementationOnce(async () => { policyState = { record: { policy: POLICY, version: 1 },
      calendar_revision: 1 }; return policyState; });
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Load pilot policy")!.click());
    expect(document.querySelectorAll(".weekly-day")).toHaveLength(7);
    expect(host.querySelector<HTMLButtonElement>('[aria-label="Edit Availability"]')!.textContent)
      .toContain("1 open day");
  });

  it("keeps an invitation draft on failed save and guards closing the editor", async () => {
    const opener = host.querySelector<HTMLButtonElement>('[aria-label="Edit Booking invitations"]')!;
    await act(async () => opener.click());
    const messageSection = [...document.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')]
      .find((button) => button.textContent?.includes("Invitation message"))!;
    expect(messageSection.getAttribute("aria-expanded")).toBe("false");
    await act(async () => messageSection.click());
    const field = document.querySelector<HTMLTextAreaElement>(".outreach-card textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(field, "Please book a visit.");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => messageSection.click());
    expect(opener.textContent).toContain("Unsaved changes");
    request.mockRejectedValueOnce(new Error("Unavailable"));
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save invitation settings")!.click());
    expect(field.value).toBe("Please book a visit.");
    expect(opener.textContent).toContain("Recurring invitations off");
    await act(async () => document.querySelector<HTMLButtonElement>(".popover-close")!.click());
    expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain("Booking invitations");
    await act(async () => [...document.querySelectorAll<HTMLButtonElement>('[role="alertdialog"] button')]
      .find((button) => button.textContent === "Keep editing")!.click());
    expect(field.value).toBe("Please book a visit.");
  });
});
