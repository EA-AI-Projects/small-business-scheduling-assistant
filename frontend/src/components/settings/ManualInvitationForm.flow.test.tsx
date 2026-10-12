// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { OutreachState } from "@/api/types";

import { OutreachForm } from "./OutreachForm";

const get = vi.fn(async () => ({ eligible: 3, examined: 4, delivery_enabled: true }));
const api = { businessId: "pilot", get };
let outreach: OutreachState | null = null;
const profile = { display_name: "Example Pet Care", description: "A synthetic pet business.",
  support_contact: "help@example.invalid", service_name: "Dog grooming",
  service_description: "A synthetic grooming visit.", default_duration_minutes: 60 };
let saved: { profile: typeof profile | null; version: number } = { profile, version: 1 };
vi.mock("@/owner/OwnerContext", () => ({
  useOwner: () => ({ api, change: vi.fn(), data: { outreach, businessProfile: saved, zone: "America/Los_Angeles" },
    notify: vi.fn(), refresh: vi.fn() }),
}));

describe("Manual booking invitation form", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    sessionStorage.clear();
    get.mockClear();
    outreach = null;
    saved = { profile, version: 1 };
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

  it("offers no default wording until the business details are saved", async () => {
    saved = { profile: null, version: 0 };
    outreach = { settings: { enabled: true, weekday: 0,
      local_time: "09:00", lookahead_weeks: 1 }, version: 1 };
    await act(async () => root.render(<OutreachForm />));
    expect(host.querySelector("textarea")?.value).toBe("");
    expect(host.textContent).toContain("Save the business name and service");
    expect(host.textContent).not.toMatch(/cleaning/i);
  });

  it.each([[1, "one week"], [2, "two weeks"]] as const)(
    "prefills the approved %s-week copy after settings load", async (weeks, window) => {
    outreach = { settings: { enabled: true, weekday: 0,
      local_time: "09:00", lookahead_weeks: weeks }, version: 1 };
    await act(async () => root.render(<OutreachForm />));
    expect(host.querySelector("textarea")?.value).toBe(
      "Example Pet Care: Would you like to book a visit for Dog grooming in the next "
      + `${window}? Reply with a day and time that works for you, or STOP to opt out.`);
    expect(host.textContent).toContain("3 of 4 clients currently eligible");
    expect([...host.querySelectorAll("button")].find((button) =>
      button.textContent === "Send Invitations Now")?.disabled).toBe(false);
  });
});
