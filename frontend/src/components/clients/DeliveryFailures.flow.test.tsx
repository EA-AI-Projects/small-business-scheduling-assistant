// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ApiError } from "@/lib/api";

import { DeliveryFailures } from "./DeliveryFailures";

const get = vi.fn();
const api = { get };
const data = { calendar: { business_id: "pilot" }, zone: "UTC" };
const stamp = { version: 1 };
vi.mock("@/owner/OwnerContext", () => ({
  useOwner: () => ({
    api, stamp, data,
  }),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : String(error),
}));

describe("Text delivery failures in Clients", () => {
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

  it("shows a neutral message when the local API has no SMS route", async () => {
    get.mockRejectedValueOnce(new ApiError("Server returned 404", 404, null, undefined));
    await act(async () => root.render(<DeliveryFailures />));
    expect(get).toHaveBeenCalledWith("/sms-delivery-failures");
    expect(host.textContent).toContain("Text delivery failures are not available here.");
    expect(host.textContent).not.toContain("Could not load");
  });

  it("still reports a server failure", async () => {
    get.mockRejectedValueOnce(new ApiError("Server returned 500", 500, null, undefined));
    await act(async () => root.render(<DeliveryFailures />));
    expect(host.textContent).toContain("Could not load text delivery failures: Server returned 500");
  });
});
