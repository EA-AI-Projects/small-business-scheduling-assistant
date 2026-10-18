// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { ClientProfile } from "@/api/types";
import { ApiError, type OwnerApi, type RequestOptions } from "@/lib/api";
import { OwnerProvider, useOwner } from "@/owner/OwnerContext";

import { ClientDetails } from "./ClientDetails";

const original: ClientProfile = {
  business_id: "pilot", client_id: "synthetic-client", name: "Synthetic Client",
  phone_e164: "+14155550101", service_address: "123 Test Street", active: true,
  version: 1, created_at: "2026-10-18T12:00:00Z", updated_at: "2026-10-18T12:00:00Z",
  phone_verified_at: null,
  email: null,
};
let profile = { ...original };
let accountState = "none";
let expiresAt: string | null = null;
let appKey = 0;
const get = vi.fn(async (path: string) => {
  if (path === "/clients") return [profile];
  if (path.endsWith("/account-invitation")) return {
    state: accountState === "pending" && expiresAt && Date.parse(expiresAt) <= Date.now()
      ? "expired" : accountState,
    expires_at: expiresAt,
  };
  if (path === "/calendar") return { revision: 1, events: [] };
  if (path === "/policy") return null;
  if (path === "/profile") return { profile: null, version: 0 };
  return [];
});
const request = vi.fn(async (path: string, options: RequestOptions) => {
  if (options.method === "PUT") profile = { ...profile, ...options.body as object, version: profile.version + 1 };
  if (path.endsWith("/account-invitation")) accountState = options.method === "DELETE" ? "revoked" : "pending";
});
const api = { get, request } as unknown as OwnerApi;

function Details() {
  const { data, loaded } = useOwner();
  return loaded ? <ClientDetails client={data.clients[0]} blankKey={1} initialSection="profile"
    onCreated={() => undefined} onDeleted={() => undefined} /> : null;
}

function setValue(element: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("client profile email", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(async () => {
    vi.clearAllMocks();
    profile = { ...original };
    accountState = "none";
    expiresAt = null;
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.append(host); root = createRoot(host);
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><Details /></OwnerProvider>));
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.useRealTimers(); });

  const email = () => host.querySelector<HTMLInputElement>('input[name="email"]')!;
  const save = () => act(async () => host.querySelector<HTMLFormElement>('form[aria-label="Client profile"]')!.requestSubmit());
  const render = () => act(async () => root.render(
    <OwnerProvider key={++appKey} api={api} notify={() => undefined}><Details /></OwnerProvider>));
  const button = (label: string) => [...host.querySelectorAll<HTMLButtonElement>("button")]
    .find((item) => item.textContent === label)!;
  const openAccount = () => act(async () => [...host.querySelectorAll<HTMLButtonElement>("button")]
    .find((item) => item.textContent?.includes("Account invitation"))!.click());
  const send = () => button("Send email invitation");

  it("saves, reloads, and clears optional profile email", async () => {
    expect(email()).not.toBeNull();
    expect(email().required).toBe(false);
    expect(email().value).toBe("");
    await act(async () => setValue(email(), "client@example.test"));
    await save();
    expect(request).toHaveBeenLastCalledWith("/clients/synthetic-client", expect.objectContaining({
      method: "PUT", body: expect.objectContaining({ email: "client@example.test" }),
    }));
    expect(email().value).toBe("client@example.test");
    await act(async () => setValue(email(), ""));
    await save();
    expect(request).toHaveBeenLastCalledWith("/clients/synthetic-client", expect.objectContaining({
      body: expect.objectContaining({ email: null }),
    }));
    expect(email().value).toBe("");
  });

  it("keeps the email draft when saving fails", async () => {
    expect(email()).not.toBeNull();
    await act(async () => setValue(email(), "edited@example.test"));
    request.mockRejectedValueOnce(new ApiError("Synthetic save failed", 422, null, undefined));
    await save();
    expect(email().value).toBe("edited@example.test");
  });

  it("requires saving email first, then sends to the displayed saved destination", async () => {
    await openAccount();
    expect(host.querySelectorAll('input[type="email"]')).toHaveLength(1);
    expect(send().disabled).toBe(true);
    expect(host.textContent).toContain("Add an email to the profile first.");
    await act(async () => setValue(email(), "client@example.test"));
    expect(send().disabled).toBe(true);
    expect(host.textContent).toContain("Save email changes before sending an invitation.");
    await save();
    const invitation = host.querySelector('form[aria-label="Client account invitation"]')!;
    expect(invitation.textContent).toContain("client@example.test");
    expect(send().disabled).toBe(false);
    await act(async () => send().click());
    expect(request).toHaveBeenLastCalledWith("/clients/synthetic-client/account-invitation",
      expect.objectContaining({ method: "POST", body: { expected_version: 2 } }));
    expect(invitation.textContent).toContain("Account: pending");
    expect(send().disabled).toBe(true);
  });

  it("preserves unsaved email and blocks sending after a save conflict refresh", async () => {
    profile = { ...original, email: "saved@example.test" };
    await render();
    await openAccount();
    await act(async () => setValue(email(), "edited@example.test"));
    request.mockRejectedValueOnce(new ApiError("Synthetic conflict", 409, "RECORD_CONFLICT", undefined));
    await save();
    expect(email().value).toBe("edited@example.test");
    expect(send().disabled).toBe(true);
    expect(host.textContent).toContain("Save email changes before sending an invitation.");
  });

  it.each(["pending", "active"])("requires explicit revocation of %s access before inviting", async (state) => {
    accountState = state;
    profile = { ...original, email: "saved@example.test" };
    await render();
    await openAccount();
    expect(host.textContent).toContain(`Account: ${state}`);
    expect(send().disabled).toBe(true);
    await act(async () => setValue(email(), "replacement@example.test"));
    await save();
    expect(host.textContent).toContain(`Account: ${state}`);
    expect(send().disabled).toBe(true);
    await act(async () => button("Revoke account access").click());
    expect(request).not.toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ method: "DELETE" }));
    await act(async () => button("Confirm revoke").click());
    expect(host.textContent).toContain("Account: revoked");
    expect(send().disabled).toBe(false);
    await act(async () => send().click());
    expect(request).toHaveBeenLastCalledWith("/clients/synthetic-client/account-invitation",
      expect.objectContaining({ method: "POST", body: { expected_version: 2 } }));
  });

  it("unlocks sending when a pending invitation expires while the panel stays open", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-18T12:00:00Z"));
    accountState = "pending";
    expiresAt = "2026-10-18T12:01:00Z";
    profile = { ...original, email: "saved@example.test" };
    await render();
    await openAccount();
    expect(send().disabled).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(61_000); });
    expect(host.textContent).toContain("Account: expired");
    expect(send().disabled).toBe(false);
  });
});
