// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { OutreachState } from "@/api/types";

import { ManualInvitationForm } from "./ManualInvitationForm";

const get = vi.fn(async () => ({ eligible: 3, examined: 4, delivery_enabled: true }));
const api = { businessId: "pilot", get };
let outreach: OutreachState | null = null;
vi.mock("@/owner/OwnerContext", () => ({
  useOwner: () => ({ api, data: { outreach }, notify: vi.fn() }),
}));

describe("Manual booking invitation form", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    sessionStorage.clear();
    get.mockClear();
    outreach = null;
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

  it.each([[1, "one week"], [2, "two weeks"]] as const)(
    "prefills the approved %s-week copy after settings load", async (weeks, window) => {
    await act(async () => root.render(<ManualInvitationForm />));
    expect(host.querySelector("textarea")?.value).toBe("");
    outreach = { settings: { enabled: true, weekday: 0,
      local_time: "09:00", lookahead_weeks: weeks }, version: 1 };
    await act(async () => root.render(<ManualInvitationForm />));
    expect(host.querySelector("textarea")?.value).toBe(
      "Smart Scheduling Assistant: Would you like to book a cleaning visit in the next "
      + `${window}? Reply with a day and time that works for you, or STOP to opt out.`);
    expect(host.textContent).toContain("3 of 4 clients currently eligible");
    expect(host.querySelector("button")?.disabled).toBe(false);
  });
});
