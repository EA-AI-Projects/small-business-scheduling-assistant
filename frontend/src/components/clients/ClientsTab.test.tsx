import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ClientNote, ClientProfile } from "@/api/types";
import { OwnerProvider, useOwner } from "@/owner/OwnerContext";
import { CLIENT, fakeApi, type Route } from "@/test/fakeApi";

import { ClientsTab } from "./ClientsTab";

const SECOND: ClientProfile = { ...CLIENT, client_id: "client-2", name: "Blake Sample",
  phone_e164: "+14155550102", home_size: "large", active: false, version: 3 };

function note(overrides: Partial<ClientNote> = {}): ClientNote {
  return { business_id: "pilot", client_id: "client-1", note_id: "note-1", appointment_id: null,
    body: "Prefers the side entrance", created_by: "owner", created_at: "2026-07-02T17:30:00Z",
    legal_hold_reason: null, ...overrides };
}

function Probe() {
  const { selectClient } = useOwner();
  return <button type="button" onClick={() => selectClient("client-1", "appt-7")}>open visit</button>;
}

function setup(route: Route) {
  const notify = vi.fn();
  const { api, calls } = fakeApi(route);
  const request = vi.spyOn(api, "request");
  render(<OwnerProvider api={api} notify={notify}><Probe /><ClientsTab /></OwnerProvider>);
  return { notify, calls, request, api };
}

const profile = () => screen.getByRole("form", { name: "Client profile" });
const noteForm = () => screen.getByRole("form", { name: "Add note" });

async function selectAvery() {
  await userEvent.click(await screen.findByRole("button", { name: "Avery Example" }));
}

describe("ClientsTab", () => {
  it("lists clients with phone, size and status", async () => {
    setup((method, path) => path === "/clients" ? { status: 200, body: [CLIENT, SECOND] } : undefined);
    await screen.findByRole("button", { name: "Avery Example" });
    expect(screen.getByText("+14155550101 · medium · active")).toBeInTheDocument();
    expect(screen.getByText("+14155550102 · large · inactive")).toBeInTheDocument();
  });

  it("says when there are no clients", async () => {
    setup((method, path) => path === "/clients" ? { status: 200, body: [] } : undefined);
    expect(await screen.findByText("No clients yet")).toBeInTheDocument();
  });

  it("selecting a client fills the profile and shows its notes", async () => {
    setup((method, path) => path === "/clients/client-1/notes" ? { status: 200, body: [
      note(),
      note({ note_id: "note-2", appointment_id: "appt-1", body: "Kept", legal_hold_reason: "Dispute" }),
    ] } : undefined);
    await selectAvery();
    const form = within(profile());
    expect(form.getByLabelText("Client ID")).toHaveValue("client-1");
    expect(form.getByLabelText("Client ID")).toHaveAttribute("readonly");
    expect(form.getByLabelText("Name")).toHaveValue("Avery Example");
    expect(form.getByLabelText("Home size")).toHaveValue("medium");
    expect(form.getByLabelText("Default minutes")).toHaveValue(120);
    expect(form.getByLabelText("Active")).toBeChecked();
    expect(await screen.findByText("Prefers the side entrance")).toBeInTheDocument();
    expect(screen.getByText("Client note · 2026-07-02 · 10:30 AM")).toBeInTheDocument();
    expect(screen.getByText("Visit appt-1 · 2026-07-02 · 10:30 AM")).toBeInTheDocument();
    expect(screen.getAllByText("Legal hold")).toHaveLength(1);
  });

  it("says when a client has no ordinary notes", async () => {
    setup((method, path) => path === "/clients/client-1/notes" ? { status: 200, body: [] } : undefined);
    await selectAvery();
    expect(await screen.findByText("No ordinary notes")).toBeInTheDocument();
  });

  it("saves an existing profile with its current version and no idempotency key", async () => {
    const { calls, request, notify } = setup((method, path) => {
      if (path === "/clients/client-1/notes") return { status: 200, body: [] };
      if (method === "PUT") return { status: 200, body: {} };
      return undefined;
    });
    await selectAvery();
    const name = within(profile()).getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "  Avery Renamed ");
    await userEvent.selectOptions(within(profile()).getByLabelText("Home size"), "large");
    await userEvent.click(within(profile()).getByLabelText("Active"));
    await userEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Client profile saved", undefined));
    expect(calls.find((call) => call.method === "PUT")).toEqual({ method: "PUT", path: "/clients/client-1",
      body: { expected_version: 1, name: "Avery Renamed", phone_e164: "+14155550101",
        service_address: "1 Example Way", home_size: "large", default_duration_minutes: 120, active: false } });
    expect(request).toHaveBeenCalledWith("/clients/client-1", expect.objectContaining({ idempotent: false }));
  });

  it("creates a new client with version 0 and then selects it", async () => {
    const created: ClientProfile = { ...CLIENT, client_id: "new client/9", name: "Casey Demo",
      phone_e164: "+14155550109", service_address: "9 Demo St", home_size: "small",
      default_duration_minutes: 60 };
    let clients = [CLIENT];
    const { calls, notify } = setup((method, path) => {
      if (path === "/clients") return { status: 200, body: clients };
      if (method === "PUT") { clients = [CLIENT, created]; return { status: 200, body: created }; }
      if (path.endsWith("/notes")) return { status: 200, body: [] };
      return undefined;
    });
    await selectAvery();
    await userEvent.click(screen.getByRole("button", { name: "New client" }));
    const form = within(profile());
    expect(form.getByLabelText("Client ID")).toHaveValue("");
    expect(form.getByLabelText("Client ID")).not.toHaveAttribute("readonly");
    await userEvent.type(form.getByLabelText("Client ID"), "new client/9");
    await userEvent.type(form.getByLabelText("Name"), "Casey Demo");
    await userEvent.type(form.getByLabelText("Phone (E.164)"), "+14155550109");
    await userEvent.type(form.getByLabelText("Service address"), "9 Demo St");
    await userEvent.type(form.getByLabelText("Default minutes"), "60");
    await userEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Client profile saved", undefined));
    expect(calls.find((call) => call.method === "PUT")).toEqual({ method: "PUT",
      path: "/clients/new%20client%2F9", body: { expected_version: 0, name: "Casey Demo",
        phone_e164: "+14155550109", service_address: "9 Demo St", home_size: "small",
        default_duration_minutes: 60, active: true } });
    await waitFor(() => expect(within(profile()).getByLabelText("Client ID")).toHaveAttribute("readonly"));
    expect(within(profile()).getByLabelText("Client ID")).toHaveValue("new client/9");
    await waitFor(() => expect(calls.some((call) =>
      call.path === "/clients/new%20client%2F9/notes")).toBe(true));
  });

  it("on a version conflict says nothing was saved and shows current data", async () => {
    let clients = [CLIENT];
    const { notify } = setup((method, path) => {
      if (path === "/clients") return { status: 200, body: clients };
      if (path.endsWith("/notes")) return { status: 200, body: [] };
      if (method === "PUT") {
        clients = [{ ...CLIENT, name: "Changed Elsewhere", version: 2 }];
        return { status: 409, body: { error: { code: "RECORD_CONFLICT",
          message: "Client profile version changed" } } };
      }
      return undefined;
    });
    await selectAvery();
    await userEvent.type(within(profile()).getByLabelText("Name"), " typo");
    await userEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith(
      "Nothing was saved: Client profile version changed. Current state has been refreshed.", true));
    await waitFor(() => expect(within(profile()).getByLabelText("Name")).toHaveValue("Changed Elsewhere"));
  });

  it("adds a note with an idempotency key, resets the form and reloads notes", async () => {
    let notes: ClientNote[] = [];
    const { calls, request, notify } = setup((method, path, body) => {
      if (path !== "/clients/client-1/notes") return undefined;
      if (method === "POST") {
        notes = [note({ body: (body as { body: string }).body, appointment_id: "appt-3" })];
        return { status: 200, body: notes[0] };
      }
      return { status: 200, body: notes };
    });
    await selectAvery();
    await screen.findByText("No ordinary notes");
    await userEvent.type(within(noteForm()).getByLabelText("Note"), " Bring ladder ");
    await userEvent.type(within(noteForm()).getByLabelText("Appointment ID (optional)"), "appt-3");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Note saved", undefined));
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ body: "Bring ladder",
      appointment_id: "appt-3" });
    expect(request).toHaveBeenCalledWith("/clients/client-1/notes", expect.objectContaining({
      method: "POST", idempotent: true }));
    expect(await screen.findByText("Bring ladder")).toBeInTheDocument();
    expect(within(noteForm()).getByLabelText("Note")).toHaveValue("");
    expect(within(noteForm()).getByLabelText("Appointment ID (optional)")).toHaveValue("");
  });

  it("sends a missing appointment ID as null", async () => {
    const { calls } = setup((method, path) => path === "/clients/client-1/notes"
      ? { status: 200, body: method === "POST" ? note() : [] } : undefined);
    await selectAvery();
    await userEvent.type(within(noteForm()).getByLabelText("Note"), "General");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    await waitFor(() => expect(calls.find((call) => call.method === "POST")?.body)
      .toEqual({ body: "General", appointment_id: null }));
  });

  it("requires a selected client before adding a note", async () => {
    const { notify, calls } = setup(() => undefined);
    await screen.findByRole("button", { name: "Avery Example" });
    await userEvent.type(within(noteForm()).getByLabelText("Note"), "Orphan");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    expect(notify).toHaveBeenCalledWith("Select a client first", true);
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("deletes a note after confirmation without a body or idempotency key", async () => {
    let notes = [note({ note_id: "n/1" })];
    const { calls, request, notify } = setup((method, path) => {
      if (method === "DELETE" && path === "/clients/client-1/notes/n%2F1") {
        notes = [];
        return { status: 200, body: null };
      }
      return path === "/clients/client-1/notes" ? { status: 200, body: notes } : undefined;
    });
    await selectAvery();
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));
    expect(calls.some((call) => call.method === "DELETE")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Confirm deletion" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Note deleted", undefined));
    expect(calls.find((call) => call.method === "DELETE")?.body).toBeUndefined();
    expect(request).toHaveBeenCalledWith("/clients/client-1/notes/n%2F1", expect.objectContaining({
      method: "DELETE", body: undefined, idempotent: false }));
    expect(await screen.findByText("No ordinary notes")).toBeInTheDocument();
  });

  it("prefills the visit when notes are opened from the schedule", async () => {
    setup((method, path) => path === "/clients/client-1/notes" ? { status: 200, body: [] } : undefined);
    await screen.findByRole("button", { name: "Avery Example" });
    await userEvent.click(screen.getByRole("button", { name: "open visit" }));
    expect(within(noteForm()).getByLabelText("Appointment ID (optional)")).toHaveValue("appt-7");
    expect(within(profile()).getByLabelText("Client ID")).toHaveValue("client-1");
  });

  it("does not carry a schedule visit over to another client", async () => {
    setup((method, path) => path === "/clients" ? { status: 200, body: [CLIENT, SECOND] }
      : path.endsWith("/notes") ? { status: 200, body: [] } : undefined);
    await screen.findByRole("button", { name: "Blake Sample" });
    await userEvent.click(screen.getByRole("button", { name: "open visit" }));
    expect(within(noteForm()).getByLabelText("Appointment ID (optional)")).toHaveValue("appt-7");
    await userEvent.click(screen.getByRole("button", { name: "Blake Sample" }));
    expect(within(noteForm()).getByLabelText("Appointment ID (optional)")).toHaveValue("");
  });

  it("prefills the same visit again after the owner cleared it", async () => {
    setup((method, path) => path === "/clients/client-1/notes" ? { status: 200, body: [] } : undefined);
    await screen.findByRole("button", { name: "Avery Example" });
    await userEvent.click(screen.getByRole("button", { name: "open visit" }));
    await userEvent.clear(within(noteForm()).getByLabelText("Appointment ID (optional)"));
    await userEvent.click(screen.getByRole("button", { name: "open visit" }));
    expect(within(noteForm()).getByLabelText("Appointment ID (optional)")).toHaveValue("appt-7");
  });

  it("ignores a notes response for a client that is no longer selected", async () => {
    const { api } = setup((method, path) => path === "/clients" ? { status: 200, body: [CLIENT, SECOND] } : undefined);
    const pending = new Map<string, (notes: ClientNote[]) => void>();
    const get = api.get.bind(api);
    vi.spyOn(api, "get").mockImplementation(<T,>(path: string) => path.endsWith("/notes")
      ? new Promise<T>((resolve) => { pending.set(path, resolve as (notes: ClientNote[]) => void); })
      : get<T>(path));
    await selectAvery();
    await userEvent.click(screen.getByRole("button", { name: "Blake Sample" }));
    await act(async () => {
      pending.get("/clients/client-2/notes")?.([note({ client_id: "client-2", body: "Blake note" })]);
    });
    await act(async () => {
      pending.get("/clients/client-1/notes")?.([note({ body: "Avery note" })]);
    });
    expect(screen.getByText("Blake note")).toBeInTheDocument();
    expect(screen.queryByText("Avery note")).not.toBeInTheDocument();
  });
});
