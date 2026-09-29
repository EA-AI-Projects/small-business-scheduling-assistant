import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { Appointment, CalendarSnapshot, UnavailableBlock } from "@/api/types";
import { OwnerProvider, useOwner } from "@/owner/OwnerContext";
import { CALENDAR, fakeApi, type Route } from "@/test/fakeApi";

import { ScheduleTab } from "./ScheduleTab";

// July 6, 2026 is a Monday; 16:00Z is 9:00 AM in Los Angeles.
const APPOINTMENT: Appointment = {
  appointment_id: "appt-1", business_id: "pilot", client_id: "client-1",
  start_at: "2026-07-06T16:00:00Z", end_at: "2026-07-06T18:00:00Z", status: "CONFIRMED",
  hold_expires_at: null, duration_minutes: 120, buffer_minutes: 30, version: 3, replaces_appointment_id: null,
};
const BLOCK: UnavailableBlock = {
  block_id: "block-1", business_id: "pilot", start_at: "2026-07-06T20:00:00Z",
  end_at: "2026-07-06T21:00:00Z", version: 2,
};
const SNAPSHOT: CalendarSnapshot = {
  ...CALENDAR, revision: 7, events: [
    { event_id: "block-1", start_at: BLOCK.start_at, end_at: BLOCK.end_at, status: "UNAVAILABLE",
      hold_expires_at: null, buffer_minutes: 0, duration_minutes: 60 },
    { event_id: "appt-1", start_at: APPOINTMENT.start_at, end_at: APPOINTMENT.end_at, status: "CONFIRMED",
      hold_expires_at: null, buffer_minutes: 30, duration_minutes: 120 },
    // 06:30Z on July 7 is still July 6 locally.
    { event_id: "late", start_at: "2026-07-07T06:30:00Z", end_at: "2026-07-07T07:00:00Z", status: "CANCELLED",
      hold_expires_at: null, buffer_minutes: 0, duration_minutes: 30 },
  ],
};

function localTimeRoute(method: string, path: string) {
  if (method === "GET" && path.startsWith("/local-time?")) {
    const value = new URLSearchParams(path.split("?")[1]).get("value") ?? "";
    if (value === "2026-03-08T02:30") {
      return { status: 422, body: { error: { message: "Local time is missing or ambiguous at DST change" } } };
    }
    // Test inputs are all in July (UTC-7).
    const [date, time] = value.split("T");
    const [hour, minute] = (time ?? "00:00").split(":").map(Number);
    const instant = new Date(`${date}T00:00:00Z`);
    instant.setUTCHours((hour ?? 0) + 7, minute ?? 0);
    return { status: 200, body: { instant: instant.toISOString() } };
  }
  return undefined;
}

function TabProbe() {
  const { tab, selectedClientId, noteAppointmentId } = useOwner();
  return <output>{`${tab}|${selectedClientId}|${noteAppointmentId}`}</output>;
}

async function setup(route: Route = () => undefined) {
  const notify = vi.fn();
  const { api, calls } = fakeApi((method, path, body) =>
    route(method, path, body) ?? localTimeRoute(method, path) ?? (
      path === "/calendar" ? { status: 200, body: SNAPSHOT }
        : path === "/appointments/appt-1" ? { status: 200, body: APPOINTMENT }
          : path === "/blocks/block-1" ? { status: 200, body: BLOCK } : undefined));
  render(<OwnerProvider api={api} notify={notify}><ScheduleTab /><TabProbe /></OwnerProvider>);
  const dateInput = await screen.findByLabelText("Start date");
  await waitFor(() => expect(dateInput).not.toHaveValue(""));
  fireEvent.change(dateInput, { target: { value: "2026-07-06" } });
  return { notify, calls, writes: () => calls.filter((call) => call.method !== "GET") };
}

describe("ScheduleTab", () => {
  it("groups events by business-local day in start order", async () => {
    await setup();
    const day = screen.getByRole("region", { name: "Monday, Jul 6" });
    const items = within(day).getAllByRole("button").map((button) => button.textContent);
    expect(items).toEqual(["9:00 AM–11:00 AMCONFIRMED", "1:00 PM–2:00 PMUNAVAILABLE", "11:30 PM–12:00 AMCANCELLED"]);
    expect(screen.getByText("Times in America/Los_Angeles · revision 7")).toBeInTheDocument();
  });

  it("shows the Monday-to-Sunday week", async () => {
    await setup();
    await userEvent.selectOptions(screen.getByLabelText("View"), "week");
    expect(screen.getAllByRole("region")).toHaveLength(7);
    expect(screen.getByRole("region", { name: "Sunday, Jul 12" })).toHaveTextContent("No scheduled items");
  });

  it("edits a confirmed appointment with its version and a resolved local start", async () => {
    const { writes } = await setup((method) => method === "PATCH" ? { status: 200, body: {} } : undefined);
    await userEvent.click(screen.getByText("9:00 AM–11:00 AM"));
    const start = await screen.findByDisplayValue("2026-07-06T09:00");
    fireEvent.change(start, { target: { value: "2026-07-06T10:15" } });
    fireEvent.change(screen.getByLabelText("Duration (minutes)"), { target: { value: "90" } });
    await userEvent.click(screen.getByText("Save appointment"));
    await waitFor(() => expect(writes()).toEqual([{ method: "PATCH", path: "/appointments/appt-1",
      body: { expected_version: 3, start_at: "2026-07-06T17:15:00.000Z", duration_minutes: 90 } }]));
    // A committed write clears the selection.
    await waitFor(() => expect(screen.getByText("Choose an appointment or block.")).toBeInTheDocument());
  });

  it("cancels only after confirmation", async () => {
    const { writes } = await setup((method) => method === "POST" ? { status: 200, body: {} } : undefined);
    await userEvent.click(screen.getByText("9:00 AM–11:00 AM"));
    await userEvent.click(await screen.findByText("Cancel appointment"));
    expect(writes()).toEqual([]);
    await userEvent.click(screen.getByText("Confirm cancellation"));
    await waitFor(() => expect(writes()).toEqual([
      { method: "POST", path: "/appointments/appt-1/cancel", body: { expected_version: 3 } }]));
  });

  it("keeps the selection and reloads current state after a conflict", async () => {
    const { notify } = await setup((method) => method === "PATCH"
      ? { status: 409, body: { error: { code: "STALE_VERSION", message: "Appointment changed" } } } : undefined);
    await userEvent.click(screen.getByText("9:00 AM–11:00 AM"));
    await userEvent.click(await screen.findByText("Save appointment"));
    await waitFor(() => expect(notify).toHaveBeenCalledWith(
      "Nothing was saved: Appointment changed. Current state has been refreshed.", true));
    expect(await screen.findByText("Save appointment")).toBeInTheDocument();
  });

  it("moves and removes a block with the calendar revision and block version", async () => {
    const { writes } = await setup((method) =>
      method === "PUT" || method === "DELETE" ? { status: 200, body: {} } : undefined);
    await userEvent.click(screen.getByText("1:00 PM–2:00 PM"));
    fireEvent.change(await screen.findByDisplayValue("2026-07-06T14:00"), { target: { value: "2026-07-06T15:00" } });
    await userEvent.click(screen.getByText("Move block"));
    await waitFor(() => expect(writes()[0]).toEqual({ method: "PUT", path: "/blocks/block-1", body: {
      expected_revision: 7, expected_version: 2, start_at: "2026-07-06T20:00:00.000Z",
      end_at: "2026-07-06T22:00:00.000Z" } }));
    await userEvent.click(screen.getByText("1:00 PM–2:00 PM"));
    await userEvent.click(await screen.findByText("Remove block"));
    await userEvent.click(screen.getByText("Confirm removal"));
    await waitFor(() => expect(writes()[1]).toEqual({ method: "DELETE", path: "/blocks/block-1",
      body: { expected_revision: 7, expected_version: 2 } }));
  });

  it("adds unavailable time and rejects an ambiguous DST time without writing", async () => {
    const { writes, notify } = await setup((method) => method === "POST" ? { status: 200, body: {} } : undefined);
    const form = screen.getByRole("button", { name: "Add block" }).closest("form")!;
    fireEvent.change(within(form).getByLabelText("Start"), { target: { value: "2026-03-08T02:30" } });
    fireEvent.change(within(form).getByLabelText("End"), { target: { value: "2026-03-08T04:00" } });
    fireEvent.submit(form);
    await waitFor(() => expect(notify).toHaveBeenCalledWith(
      "Nothing was saved: Local time is missing or ambiguous at DST change", true));
    expect(writes()).toEqual([]);

    fireEvent.change(within(form).getByLabelText("Start"), { target: { value: "2026-07-08T09:00" } });
    fireEvent.change(within(form).getByLabelText("End"), { target: { value: "2026-07-08T12:00" } });
    fireEvent.submit(form);
    await waitFor(() => expect(writes()).toEqual([{ method: "POST", path: "/blocks", body: {
      expected_revision: 7, start_at: "2026-07-08T16:00:00.000Z", end_at: "2026-07-08T19:00:00.000Z" } }]));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Unavailable time added", undefined));
  });

  it("opens the client's notes for this visit", async () => {
    await setup();
    await userEvent.click(screen.getByText("9:00 AM–11:00 AM"));
    expect(await screen.findByText(/Client Avery Example \(client-1\)/)).toBeInTheDocument();
    await userEvent.click(screen.getByText("Open client and visit notes"));
    expect(screen.getByText("clients|client-1|appt-1")).toBeInTheDocument();
  });
});
