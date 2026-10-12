// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { BusinessProfileState } from "@/api/types";
import { ApiError } from "@/lib/api";

import { BusinessProfileForm } from "./BusinessProfileForm";

const PROFILE = { display_name: "Example Cleaning", description: "Home cleaning.",
  support_contact: "help@example.test", service_name: "Standard clean",
  service_description: "A two-hour clean.", default_duration_minutes: 120 };
const notify = vi.fn();
const get = vi.fn();
const request = vi.fn();
const api = { get, request };
let maximum = 240;
vi.mock("@/owner/OwnerContext", () => ({
  useOwner: () => ({ api, notify, data: { policy: { record: { policy: { maximum_visit_minutes: maximum } } } } }),
}));

function setValue(element: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement : HTMLInputElement;
  Object.getOwnPropertyDescriptor(proto.prototype, "value")!.set!.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("Business details settings", () => {
  let host: HTMLDivElement;
  let root: Root;
  const field = (label: string) => [...host.querySelectorAll("label")]
    .find((item) => item.textContent?.startsWith(label))!.querySelector<HTMLInputElement>("input, textarea")!;
  const click = async (text: string) => act(async () => [...host.querySelectorAll<HTMLButtonElement>("button")]
    .find((button) => button.textContent === text)!.click());

  async function mount(state: BusinessProfileState) {
    get.mockResolvedValue(state);
    await act(async () => root.render(<BusinessProfileForm />));
  }

  beforeEach(() => {
    get.mockReset();
    request.mockReset();
    notify.mockReset();
    maximum = 240;
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

  it("shows saved values, flags unsaved edits, and saves with the current version", async () => {
    await mount({ profile: PROFILE, version: 3 });
    expect(get).toHaveBeenCalledWith("/profile");
    expect(field("Business name").value).toBe("Example Cleaning");
    expect(host.textContent).toContain("Existing appointments keep the duration");
    expect(host.textContent).not.toContain("unsaved changes");
    request.mockResolvedValue({ profile: { ...PROFILE, default_duration_minutes: 90 }, version: 4 });
    await act(async () => setValue(field("Default booking duration"), "90"));
    expect(host.textContent).toContain("unsaved changes");
    await click("Save business details");
    expect(request).toHaveBeenCalledWith("/profile", { method: "PUT", idempotent: true,
      body: { expected_version: 3, profile: { ...PROFILE, default_duration_minutes: 90 } } });
    expect(host.textContent).toContain("Version 4");
    expect(host.textContent).not.toContain("unsaved changes");
    expect(notify).toHaveBeenCalledWith("Business details saved");
  });

  it("validates before sending, including the policy maximum visit", async () => {
    await mount({ profile: null, version: 0 });
    expect(host.textContent).toContain("Not set up yet");
    await click("Save business details");
    expect(request).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Enter a business name.");
    await act(async () => setValue(field("Default booking duration"), "300"));
    expect(host.textContent).toContain("cannot exceed the current maximum visit of 240 minutes");
  });

  it("keeps edits and shows the newer version after a stale save", async () => {
    await mount({ profile: PROFILE, version: 3 });
    request.mockRejectedValueOnce(new ApiError("stale", 409, "STALE_VERSION",
      { profile: { ...PROFILE, description: "Changed elsewhere." }, version: 5 }));
    await act(async () => setValue(field("Business name"), "Renamed Cleaning"));
    await click("Save business details");
    expect(host.textContent).toContain("changed elsewhere (now version 5)");
    expect(host.textContent).toContain("Version 5");
    expect(field("Business name").value).toBe("Renamed Cleaning");
    request.mockResolvedValueOnce({ profile: { ...PROFILE, display_name: "Renamed Cleaning" }, version: 6 });
    await click("Save business details");
    expect(request).toHaveBeenLastCalledWith("/profile", expect.objectContaining({
      body: expect.objectContaining({ expected_version: 5 }) }));
    expect(host.textContent).toContain("Version 6");
    expect(host.textContent).not.toContain("changed elsewhere");
  });

  it("shows a server rejection without losing edits", async () => {
    await mount({ profile: PROFILE, version: 1 });
    request.mockRejectedValueOnce(new ApiError("Duration exceeds the maximum visit", 422, null, undefined));
    await act(async () => setValue(field("Service name"), "Deep clean"));
    await click("Save business details");
    expect(host.textContent).toContain("Duration exceeds the maximum visit");
    expect(field("Service name").value).toBe("Deep clean");
  });
});
