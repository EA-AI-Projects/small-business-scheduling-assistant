import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { SmsDeliveryFailure } from "@/api/types";
import { OwnerProvider } from "@/owner/OwnerContext";
import { fakeApi, type Route } from "@/test/fakeApi";

import { ClientsTab } from "./ClientsTab";

const FAILURE: SmsDeliveryFailure = { business_id: "pilot", outbox_id: "out-1", provider_id: "SM1",
  status: "failed", recipient: "+14155550101", observed_at: "2026-07-02T17:30:00Z", error_code: "30007" };

function setup(route: Route) {
  const notify = vi.fn();
  const { api, calls } = fakeApi((method, path, body) => {
    if (path.endsWith("/notes")) return { status: 200, body: [] };
    return route(method, path, body);
  });
  render(<OwnerProvider api={api} notify={notify}><ClientsTab /></OwnerProvider>);
  return { notify, calls };
}

const consent = () => screen.getByRole("form", { name: "Record in-person text consent" });

async function selectAvery() {
  await userEvent.click(await screen.findByRole("button", { name: "Avery Example" }));
}

describe("in-person text consent", () => {
  it("prefills the participant name and states that the full record is kept outside the app", async () => {
    setup(() => undefined);
    await selectAvery();
    expect(within(consent()).getByLabelText("Participant name")).toHaveValue("Avery Example");
    expect(screen.getByText(/full private consent record is kept by the owner outside this app/))
      .toBeInTheDocument();
  });

  it("does not post until they clearly said yes", async () => {
    const { calls, notify } = setup(() => undefined);
    await selectAvery();
    await userEvent.type(within(consent()).getByLabelText("Consent script version"), "script-a");
    await userEvent.click(screen.getByRole("button", { name: "Record consent" }));
    expect(within(consent()).getByLabelText("They clearly said yes")).toBeRequired();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
    expect(notify).not.toHaveBeenCalledWith("Text consent recorded", undefined);
  });

  it("records consent for the selected client without an idempotency key", async () => {
    const { calls, notify } = setup((method, path) =>
      method === "POST" && path === "/clients/client-1/sms-consent" ? { status: 200, body: {} } : undefined);
    await selectAvery();
    const form = within(consent());
    await userEvent.clear(form.getByLabelText("Participant name"));
    await userEvent.type(form.getByLabelText("Participant name"), "Synthetic Client");
    await userEvent.type(form.getByLabelText("Consent script version"), "script-a");
    await userEvent.click(form.getByLabelText("They clearly said yes"));
    await userEvent.click(screen.getByRole("button", { name: "Record consent" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Text consent recorded", undefined));
    expect(calls.find((call) => call.method === "POST")).toEqual({ method: "POST",
      path: "/clients/client-1/sms-consent", body: { phone_e164: "+14155550101",
        participant_name: "Synthetic Client", script_version: "script-a", clear_yes: true } });
  });

  it("shows the server's reason when consent is rejected", async () => {
    const { notify } = setup((method) => method === "POST" ? { status: 422, body: { error: {
      code: "INVALID_CONSENT", message: "Consent phone must match an active client profile" } } } : undefined);
    await selectAvery();
    await userEvent.type(within(consent()).getByLabelText("Consent script version"), "script-a");
    await userEvent.click(within(consent()).getByLabelText("They clearly said yes"));
    await userEvent.click(screen.getByRole("button", { name: "Record consent" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith(
      "Nothing was saved: Consent phone must match an active client profile", true));
  });
});

describe("text delivery failures", () => {
  it("lists failed texts", async () => {
    setup((method, path) => path === "/sms-delivery-failures" ? { status: 200, body: [FAILURE] } : undefined);
    expect(await screen.findByText("+14155550101", { selector: "div:not(.meta)" })).toBeInTheDocument();
    expect(screen.getByText(/failed \(code 30007\)/)).toBeInTheDocument();
  });

  it("says when there are none", async () => {
    setup((method, path) => path === "/sms-delivery-failures" ? { status: 200, body: [] } : undefined);
    expect(await screen.findByText("No failed texts")).toBeInTheDocument();
  });

  it("explains when the list cannot load", async () => {
    setup((method, path) => path === "/sms-delivery-failures"
      ? { status: 404, body: { error: { message: "Not Found" } } } : undefined);
    expect(await screen.findByText("Could not load text delivery failures: Not Found")).toBeInTheDocument();
  });
});
