// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { OutreachState } from "@/api/types";

import { OutreachForm } from "./OutreachForm";

const change = vi.fn(async () => true);
let outreach: OutreachState = { settings: { enabled: false, weekday: null,
  local_time: null, lookahead_weeks: null }, version: 0 };
vi.mock("@/owner/OwnerContext", () => ({
  useOwner: () => ({ change, data: { outreach, zone: "America/Los_Angeles" } }),
}));

describe("Booking invitation settings", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(async () => {
    change.mockClear();
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
    await act(async () => host.querySelector<HTMLButtonElement>("button")!.click());
    expect(change).toHaveBeenCalledWith("/booking-outreach", "PUT", {
      expected_version: 0, settings: { enabled: true, weekday: 0,
        local_time: "09:00", lookahead_weeks: 2 },
    }, "Booking invitation settings saved");
  });
});
