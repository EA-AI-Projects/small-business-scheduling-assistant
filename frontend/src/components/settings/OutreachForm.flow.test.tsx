// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { OutreachState } from "@/api/types";

import { OutreachForm } from "./OutreachForm";

const change = vi.fn(async () => true);
const get = vi.fn(async () => ({ eligible: 3, examined: 4, delivery_enabled: true }));
const api = { businessId: "pilot", get, request: vi.fn(async (_path: string, options: {
  body: { settings: OutreachState["settings"] }
}) => ({ settings: options.body.settings, version: 2 })) };
let outreach: OutreachState = { settings: { enabled: false, weekday: null,
  local_time: null, lookahead_weeks: null }, version: 0 };
vi.mock("@/owner/OwnerContext", () => ({
  useOwner: () => ({ api, change, notify: vi.fn(), refresh: vi.fn(),
    data: { outreach, zone: "America/Los_Angeles" } }),
}));

describe("Booking invitation settings", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(async () => {
    change.mockClear();
    get.mockClear();
    api.request.mockClear();
    outreach = { settings: { enabled: false, weekday: null,
      local_time: null, lookahead_weeks: null }, version: 0 };
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root.render(<OutreachForm />));
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

  it("explains eligibility and saves a selected schedule with the current version", async () => {
    expect(host.textContent).toContain("no confirmed appointment");
    expect(host.textContent).toContain("pending request does not count");
    expect(host.textContent).toContain("at most one invitation");
    await act(async () => host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    const select = host.querySelectorAll<HTMLSelectElement>("select");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select[0], "0");
      select[0]!.dispatchEvent(new Event("change", { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select[1], "2");
      select[1]!.dispatchEvent(new Event("change", { bubbles: true }));
      const time = host.querySelector<HTMLInputElement>('input[type="time"]')!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(time, "09:00");
      time.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => [...host.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save invitation settings")!.click());
    expect(change).toHaveBeenCalledWith("/booking-outreach", "PUT", {
      expected_version: 0, settings: { enabled: true, weekday: 0,
        local_time: "09:00", lookahead_weeks: 2,
        message: "Smart Scheduling Assistant: Would you like to book a cleaning visit in the next "
          + "two weeks? Reply with a day and time that works for you, or STOP to opt out." },
    }, "Booking invitation settings saved");
  });

  it("allows custom text without STOP for the shared invitation message", async () => {
    const textarea = host.querySelector<HTMLTextAreaElement>("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(
        textarea, "Please book a cleaning visit.");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => [...host.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Save invitation settings")!.click());
    expect(change).toHaveBeenCalledWith("/booking-outreach", "PUT", expect.objectContaining({
      settings: expect.objectContaining({ message: "Please book a cleaning visit." }),
    }), "Booking invitation settings saved");
  });

  it("keeps the new settings version after cancelling a manual send", async () => {
    outreach = { settings: { enabled: true, weekday: 0, local_time: "09:00",
      lookahead_weeks: 1, message: "Old text" }, version: 1 };
    await act(async () => root.render(<OutreachForm />));
    const textarea = host.querySelector<HTMLTextAreaElement>("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(
        textarea, "New text without opt-out phrase");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await act(async () => [...host.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Send Invitations Now")!.click());
    expect(api.request).toHaveBeenCalledWith("/booking-outreach", expect.objectContaining({
      body: expect.objectContaining({ expected_version: 1 }),
    }));
    expect(host.textContent).toContain("Version 2");
    vi.mocked(window.confirm).mockRestore();
  });
});
