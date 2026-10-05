// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { ClientProfile } from "@/api/types";

import { ConsentForm } from "./ConsentForm";

const change = vi.fn(async () => true);
const notify = vi.fn();
vi.mock("@/owner/OwnerContext", () => ({
  useOwner: () => ({ change, notify, data: { zone: "America/Los_Angeles" } }),
}));

const base = { client_id: "synthetic-client", name: "Synthetic Client",
  phone_e164: "+14155550101" } as ClientProfile;
const consented = { ...base, phone_verified_at: "2026-09-28T15:30:00+00:00" } as ClientProfile;

describe("Text Consent status", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
  });

  const render = (client: ClientProfile, phoneUnsaved = false) =>
    act(async () => root.render(<ConsentForm client={client} phoneUnsaved={phoneUnsaved} />));
  const status = () => host.querySelector('[data-testid="consent-status"]')?.textContent;
  const checkbox = () => host.querySelector<HTMLInputElement>('input[name="clear_yes"]')!;

  it("shows the recorded date and saved phone for a consented client, checkbox unchecked", async () => {
    await render(consented);
    expect(status()).toBe("Consent recorded on 2026-09-28 for +14155550101.");
    expect(checkbox().checked).toBe(false);
    expect(status()).not.toMatch(/enabled|STOP|opt/i);
  });

  it("shows the recorded date in the business timezone", async () => {
    // 01:30 UTC on Sep 29 is 6:30 PM PDT on Sep 28.
    await render({ ...base, phone_verified_at: "2026-09-29T01:30:00+00:00" } as ClientProfile);
    expect(status()).toBe("Consent recorded on 2026-09-28 for +14155550101.");
  });

  it("shows no consent for an unconsented client", async () => {
    await render({ ...base, phone_verified_at: null } as ClientProfile);
    expect(status()).toBe("No consent recorded for this phone.");
    expect(checkbox().checked).toBe(false);
  });

  it("records consent, then reflects the refreshed profile", async () => {
    await render({ ...base, phone_verified_at: null } as ClientProfile);
    await act(async () => checkbox().click());
    await act(async () => host.querySelector("form")!.requestSubmit());
    expect(change).toHaveBeenCalledOnce();
    expect(checkbox().checked).toBe(false);
    await render(consented);
    expect(status()).toContain("Consent recorded");
    expect(checkbox().checked).toBe(false);
  });

  it("clears the status when the refreshed profile has a changed phone", async () => {
    await render(consented);
    await render({ ...base, phone_e164: "+14155550102", phone_verified_at: null } as ClientProfile);
    expect(status()).toBe("No consent recorded for this phone.");
  });
});
