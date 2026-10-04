import { OwnerApi } from "@/lib/api";
import { parseConfig } from "@/lib/config";

import { deleteClientFlow } from "./deleteClientFlow";

const config = parseConfig({ authMode: "local", apiBaseUrl: "http://127.0.0.1:8000", businessId: "pilot" });

function actions() {
  return {
    clearSelection: vi.fn(), close: vi.fn(), refresh: vi.fn(async () => undefined), notify: vi.fn(),
  };
}

describe("owner client deletion flow", () => {
  it("clears stale selection and refreshes clients and calendar only after 204", async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
    const api = new OwnerApi(config, "tok", () => undefined, fetcher);
    const steps = actions();

    expect(await deleteClientFlow(api, "client/1", steps)).toBeNull();
    expect(fetcher).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/v1/owner/businesses/pilot/clients/client%2F1",
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(steps.clearSelection).toHaveBeenCalledOnce();
    expect(steps.close).toHaveBeenCalledOnce();
    expect(steps.refresh).toHaveBeenCalledOnce();
    expect(steps.notify).toHaveBeenCalledWith("Client deleted");
  });

  it("keeps selection and confirmation available after an uncertain failure", async () => {
    const fetcher = vi.fn(async () => { throw new TypeError("offline"); });
    const steps = actions();
    const result = await deleteClientFlow(new OwnerApi(config, "tok", () => undefined, fetcher), "client", steps);
    expect(result).toContain("Deletion could not be confirmed");
    expect(steps.clearSelection).not.toHaveBeenCalled();
    expect(steps.close).not.toHaveBeenCalled();
    expect(steps.refresh).not.toHaveBeenCalled();
    expect(steps.notify).not.toHaveBeenCalled();
  });

  it.each([200, 202])("does not report deletion complete for HTTP %i", async (status) => {
    const steps = actions();
    const api = new OwnerApi(config, "tok", () => undefined,
      async () => new Response("{}", { status }));
    const result = await deleteClientFlow(api, "client", steps);
    expect(result).toContain(`expected 204`);
    expect(steps.clearSelection).not.toHaveBeenCalled();
    expect(steps.close).not.toHaveBeenCalled();
    expect(steps.refresh).not.toHaveBeenCalled();
    expect(steps.notify).not.toHaveBeenCalled();
  });

  it("reports a failed refresh as deleted without claiming the view is current", async () => {
    const steps = actions();
    steps.refresh.mockRejectedValueOnce(new Error("offline"));
    const api = new OwnerApi(config, "tok", () => undefined,
      async () => new Response(null, { status: 204 }));
    expect(await deleteClientFlow(api, "client", steps)).toBeNull();
    expect(steps.close).toHaveBeenCalledOnce();
    expect(steps.notify).toHaveBeenCalledWith(expect.stringContaining("latest calendar and client list could not load"), true);
  });
});
