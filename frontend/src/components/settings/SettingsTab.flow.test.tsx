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
const get = vi.fn(async (path: string) => {
  if (path === "/profile") return profileState;
  if (path === "/policy") return { record: { policy: POLICY, version: 1 }, calendar_revision: 1 };
  if (path === "/booking-outreach") return { version: 1, settings: { enabled: false,
    weekday: null, local_time: null, lookahead_weeks: null, message: null } };
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
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><SettingsTab /></OwnerProvider>));
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });

  it("shows four saved-value cards and opens Business details with collapsible sections", async () => {
    const cards = [...host.querySelectorAll<HTMLButtonElement>('button[aria-haspopup="dialog"]')];
    expect(cards).toHaveLength(4);
    expect(cards.map((card) => card.textContent)).toEqual([
      expect.stringContaining("Business details"), expect.stringContaining("Availability"),
      expect.stringContaining("Advanced scheduling"), expect.stringContaining("Booking invitations"),
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
      { label: "Edit Availability", expected: "Monday: 08:00–17:00" },
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
});
