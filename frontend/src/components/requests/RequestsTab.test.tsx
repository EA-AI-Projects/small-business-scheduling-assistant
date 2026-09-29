import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { Appointment } from "@/api/types";
import { OwnerProvider } from "@/owner/OwnerContext";
import { fakeApi, type Route } from "@/test/fakeApi";

import { RequestsTab } from "./RequestsTab";

const REQUEST: Appointment = {
  appointment_id: "req-1", business_id: "pilot", client_id: "client-1",
  start_at: "2026-07-07T17:00:00Z", end_at: "2026-07-07T18:00:00Z", status: "PENDING_APPROVAL",
  hold_expires_at: "2026-07-06T17:00:00Z", duration_minutes: 60, buffer_minutes: 30, version: 1,
  replaces_appointment_id: "appt-0",
};

function setup(requests: Appointment[], route: Route = () => undefined) {
  const notify = vi.fn();
  const { api, calls } = fakeApi((method, path, body) =>
    route(method, path, body) ?? (path === "/requests" ? { status: 200, body: requests } : undefined));
  render(<OwnerProvider api={api} notify={notify}><RequestsTab /></OwnerProvider>);
  return { notify, writes: () => calls.filter((call) => call.method !== "GET") };
}

describe("RequestsTab", () => {
  it("shows an empty state", async () => {
    setup([]);
    expect(await screen.findByText("No pending requests")).toBeInTheDocument();
  });

  it("describes each request in the business timezone", async () => {
    setup([REQUEST]);
    expect(await screen.findByText("2026-07-07 · 10:00 AM–11:00 AM")).toBeInTheDocument();
    expect(screen.getByText(
      "Client Avery Example (client-1) · 60 minutes · expires 2026-07-06 · 10:00 AM")).toBeInTheDocument();
    expect(screen.getByText("Replacement request; original visit stays until approval.")).toBeInTheDocument();
  });

  it.each([["Approve", "Confirm approval", "approve", "Request approved"],
    ["Decline", "Confirm decline", "decline", "Request declined"]])(
    "%s sends the exact request version after confirmation", async (label, confirm, action, message) => {
      const { writes, notify } = setup([REQUEST], (method) => method === "POST" ? { status: 200, body: {} } : undefined);
      await userEvent.click(await screen.findByText(label));
      expect(writes()).toEqual([]);
      await userEvent.click(screen.getByText(confirm));
      await waitFor(() => expect(writes()).toEqual([
        { method: "POST", path: `/requests/req-1/${action}`, body: { expected_version: 1 } }]));
      await waitFor(() => expect(notify).toHaveBeenCalledWith(message, undefined));
    });
});
