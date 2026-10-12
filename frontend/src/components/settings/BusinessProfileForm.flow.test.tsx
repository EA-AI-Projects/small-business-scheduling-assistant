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
  const field = (label: string) => {
    const item = [...host.querySelectorAll("label")].find((node) => node.textContent?.startsWith(label))!;
    return host.querySelector<HTMLInputElement>(`#${item.htmlFor}`)!;
  };
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
    expect(request).toHaveBeenCalledWith("/profile", { method: "PUT", idempotencyKey: expect.any(String),
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
    expect(host.querySelectorAll('[role="alert"]')).toHaveLength(1);
    expect(host.querySelector('[role="alert"]')!.textContent).toBe("Fix 6 fields before saving.");
    expect(field("Business name").getAttribute("aria-invalid")).toBe("true");
    const describedBy = field("Business name").getAttribute("aria-describedby")!;
    expect(host.querySelector(`#${describedBy}`)!.textContent).toBe("Enter a business name.");
    await act(async () => setValue(field("Default booking duration"), "300"));
    expect(host.textContent).toContain("cannot exceed the current maximum visit of 240 minutes");
    expect(host.querySelectorAll('[role="alert"]')).toHaveLength(1);
    const duration = field("Default booking duration").getAttribute("aria-describedby")!.split(" ");
    expect(host.querySelector(`#${duration[1]}`)!.textContent).toContain("Existing appointments keep");
  });

  it("counts characters as the server does", async () => {
    await mount({ profile: PROFILE, version: 1 });
    request.mockResolvedValue({ profile: PROFILE, version: 2 });
    await act(async () => setValue(field("Business description"), `${"a".repeat(299)}\u{1F600}`));
    await click("Save business details");
    expect(request).toHaveBeenCalledTimes(1);
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

  it("reuses the idempotency key when retrying an unchanged save after a lost response", async () => {
    await mount({ profile: PROFILE, version: 3 });
    await act(async () => setValue(field("Service name"), "Deep clean"));
    request.mockRejectedValueOnce(new ApiError("The owner API could not be reached", 0, null, undefined));
    await click("Save business details");
    request.mockResolvedValueOnce({ profile: { ...PROFILE, service_name: "Deep clean" }, version: 4 });
    await click("Save business details");
    const keys = request.mock.calls.map((call) => call[1].idempotencyKey);
    expect(keys[1]).toBe(keys[0]);
    await act(async () => setValue(field("Service name"), "Express clean"));
    request.mockResolvedValueOnce({ profile: { ...PROFILE, service_name: "Express clean" }, version: 5 });
    await click("Save business details");
    expect(request.mock.calls[2]![1].idempotencyKey).not.toBe(keys[0]);
  });

  it("keeps text typed while a save is in flight", async () => {
    await mount({ profile: PROFILE, version: 1 });
    let finish!: (value: BusinessProfileState) => void;
    request.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    await act(async () => setValue(field("Service name"), "Deep clean"));
    await click("Save business details").then(() => undefined);
    await act(async () => setValue(field("Service name"), "Deep clean plus"));
    await act(async () => finish({ profile: { ...PROFILE, service_name: "Deep clean" }, version: 2 }));
    expect(field("Service name").value).toBe("Deep clean plus");
    expect(host.textContent).toContain("unsaved changes");
  });
});
