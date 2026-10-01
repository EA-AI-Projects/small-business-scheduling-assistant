import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ClientProfile, SmsDeliveryFailure } from "@/api/types";
import { OwnerProvider } from "@/owner/OwnerContext";
import { CLIENT, fakeApi, type Route } from "@/test/fakeApi";

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
    expect(within(consent()).getByLabelText("Participant name")).toHaveAttribute("readonly");
    expect(screen.getByText(/record the real name only in your private consent record/)).toBeInTheDocument();
    expect(screen.getByText(/the time saved is the consent time/)).toBeInTheDocument();
    expect(screen.getByText("Read this number back to them: +14155550101")).toBeInTheDocument();
    expect(screen.getByText("Script version 1 (September 27, 2026)")).toBeInTheDocument();
    expect(screen.getByText(/full private consent record is kept by the owner outside this app/))
      .toBeInTheDocument();
  });

  it("does not post until they clearly said yes", async () => {
    const { calls, notify } = setup(() => undefined);
    await selectAvery();
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
    expect(form.getByLabelText("Participant name")).toHaveAttribute("readonly");
    await userEvent.click(form.getByLabelText("They clearly said yes"));
    await userEvent.click(screen.getByRole("button", { name: "Record consent" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Text consent recorded", undefined));
    expect(calls.find((call) => call.method === "POST")).toEqual({ method: "POST",
      path: "/clients/client-1/sms-consent", body: { phone_e164: "+14155550101",
        participant_name: "Avery Example", script_version: "1", clear_yes: true } });
  });

  it("shows the server's reason when consent is rejected", async () => {
    const { notify } = setup((method) => method === "POST" ? { status: 422, body: { error: {
      code: "INVALID_CONSENT", message: "Consent phone must match an active client profile" } } } : undefined);
    await selectAvery();
    await userEvent.click(within(consent()).getByLabelText("They clearly said yes"));
    await userEvent.click(screen.getByRole("button", { name: "Record consent" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith(
      "Nothing was saved: Consent phone must match an active client profile", true));
  });
});

const NEW: ClientProfile = { ...CLIENT, client_id: "client-9", name: "Casey Demo",
  phone_e164: "+14155550109", version: 1 };

async function createClient(route: Route) {
  let clients: ClientProfile[] = [];
  const result = setup((method, path, body) => {
    if (path === "/clients") return { status: 200, body: clients };
    if (method === "PUT") { clients = [NEW]; return { status: 200, body: NEW }; }
    if (method === "POST") {
      clients = [{ ...NEW, version: 2, phone_verified_at: "2026-07-02T17:30:00Z" }];
      return { status: 200, body: {} };
    }
    return route(method, path, body);
  });
  await userEvent.click(await screen.findByRole("button", { name: "New client" }));
  const form = within(screen.getByRole("form", { name: "Client profile" }));
  await userEvent.type(form.getByLabelText("Client ID"), "client-9");
  await userEvent.type(form.getByLabelText("Name"), "Casey Demo");
  await userEvent.type(form.getByLabelText("Phone (E.164)"), "+14155550109");
  await userEvent.type(form.getByLabelText("Service address"), "1 Example Way");
  await userEvent.type(form.getByLabelText("Default minutes"), "60");
  await userEvent.click(screen.getByRole("button", { name: "Save profile" }));
  return result;
}

describe("onboarding consent step", () => {
  it("is shown right after a new client is created, explains skipping, and ends once recorded", async () => {
    const { calls } = await createClient(() => undefined);
    expect(await screen.findByText("Onboarding: record in-person text consent")).toBeInTheDocument();
    expect(screen.getByText(/record it later from this screen; until then, this client cannot text/)).toBeInTheDocument();
    expect(screen.getByTestId("phone-status")).toHaveTextContent("Phone not verified");
    await userEvent.click(within(consent()).getByLabelText("They clearly said yes"));
    await userEvent.click(screen.getByRole("button", { name: "Record consent" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    await waitFor(() => expect(screen.getByTestId("phone-status")).toHaveTextContent("Phone verified"));
    expect(screen.queryByText("Onboarding: record in-person text consent")).not.toBeInTheDocument();
    expect(screen.getByText("Record in-person text consent")).toBeInTheDocument();
  });

  it("can be postponed with Record later, leaving the client unverified", async () => {
    const { calls } = await createClient(() => undefined);
    await userEvent.click(await screen.findByRole("button", { name: "Record later" }));
    expect(screen.queryByText("Onboarding: record in-person text consent")).not.toBeInTheDocument();
    expect(screen.getByTestId("phone-status")).toHaveTextContent("Phone not verified");
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("keeps consent enabled when the phone field holds a formatted or marked copy of the saved number", async () => {
    setup(() => undefined);
    await selectAvery();
    const phone = screen.getByLabelText("Phone (E.164)");
    await userEvent.clear(phone);
    await userEvent.type(phone, "+1 (415) 555-0101");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record consent" })).toBeEnabled();
    await userEvent.clear(phone);
    await userEvent.paste("\u202A+14155550101\u202C");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record consent" })).toBeEnabled();
  });

  it("shows the saved number read-only and blocks consent while the phone edit is unsaved", async () => {
    const { calls } = setup(() => undefined);
    await selectAvery();
    expect(within(consent()).getByLabelText("Saved phone number")).toHaveValue("+14155550101");
    expect(within(consent()).getByLabelText("Saved phone number")).toHaveAttribute("readonly");
    const phone = screen.getByLabelText("Phone (E.164)");
    await userEvent.clear(phone);
    await userEvent.type(phone, "+14155550188");
    expect(screen.getByRole("alert")).toHaveTextContent("Save the profile first");
    expect(screen.getByRole("button", { name: "Record consent" })).toBeDisabled();
    expect(within(consent()).getByLabelText("Saved phone number")).toHaveValue("+14155550101");
    await userEvent.clear(phone);
    await userEvent.type(phone, "+14155550101");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record consent" })).toBeEnabled();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("is not shown when editing an existing client", async () => {
    setup(() => undefined);
    await selectAvery();
    expect(screen.queryByText("Onboarding: record in-person text consent")).not.toBeInTheDocument();
    expect(screen.getByTestId("phone-status")).toHaveTextContent("Phone not verified");
  });

  it("shows when the phone is verified for texting", async () => {
    setup((method, path) => path === "/clients" ? { status: 200, body: [
      { ...CLIENT, phone_verified_at: "2026-07-02T17:30:00Z" }] } : undefined);
    await selectAvery();
    expect(screen.getByTestId("phone-status")).toHaveTextContent("Phone verified for texting");
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
