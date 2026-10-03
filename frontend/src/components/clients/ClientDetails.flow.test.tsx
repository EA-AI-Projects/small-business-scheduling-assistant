// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { ClientProfile } from "@/api/types";

import { ClientDetails } from "./ClientDetails";

const request = vi.fn(async () => undefined);
const refresh = vi.fn(async () => undefined);
const notify = vi.fn();
const selectClient = vi.fn();
vi.mock("@/owner/OwnerContext", () => ({
  useOwner: () => ({ api: { request }, refresh, notify, selectClient, stamp: { version: 1 } }),
}));
vi.mock("./ProfileForm", () => ({ ProfileForm: () => null }));
vi.mock("./ConsentForm", () => ({ ConsentForm: () => null }));
vi.mock("./NoteList", () => ({ NoteList: () => null }));
vi.mock("./NoteForm", () => ({ NoteForm: () => null }));

const client = { client_id: "synthetic-client", name: "Synthetic Client", phone_verified_at: null } as ClientProfile;

describe("Delete client confirmation", () => {
  let host: HTMLDivElement;
  let root: Root;
  const onDeleted = vi.fn();

  beforeEach(async () => {
    vi.clearAllMocks();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root.render(<ClientDetails client={client} blankKey={1}
      initialSection="profile" onCreated={() => undefined} onDeleted={onDeleted} />));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
  });

  async function click(label: string) {
    const button = [...host.querySelectorAll("button")].find((item) => item.textContent?.trim() === label);
    expect(button).toBeDefined();
    await act(async () => button!.click());
  }

  it("states irreversible effects and lets the owner cancel without a request", async () => {
    await click("Delete client");
    expect(host.textContent).toContain("This cannot be undone");
    expect(host.textContent).toContain("Future appointments and pending requests are cancelled");
    expect(host.textContent).toContain("consent records");
    await click("Cancel");
    expect(request).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain("This cannot be undone");
  });

  it("deletes only after confirmation and clears the selected client", async () => {
    await click("Delete client");
    expect(request).not.toHaveBeenCalled();
    await click("Permanently delete client");
    expect(request).toHaveBeenCalledWith("/clients/synthetic-client", { method: "DELETE", expectedStatus: 204 });
    expect(selectClient).toHaveBeenCalledWith(null);
    expect(onDeleted).toHaveBeenCalledOnce();
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("shows an uncertain failure and allows a confirmed retry", async () => {
    request.mockRejectedValueOnce(new Error("connection interrupted"));
    await click("Delete client");
    await click("Permanently delete client");
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("Deletion could not be confirmed");
    expect(onDeleted).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
    await click("Permanently delete client");
    expect(request).toHaveBeenCalledTimes(2);
    expect(onDeleted).toHaveBeenCalledOnce();
  });
});
