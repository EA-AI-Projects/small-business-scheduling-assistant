import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { PolicyState } from "@/api/types";
import { OwnerProvider } from "@/owner/OwnerContext";
import { fakeApi, POLICY, type Route } from "@/test/fakeApi";

import { SettingsTab } from "./SettingsTab";

const WITH_EXCEPTIONS: PolicyState = { ...POLICY, record: { ...POLICY.record, policy: {
  ...POLICY.record.policy, date_exceptions: {
    "2026-12-26": [{ opens: "09:00:00", closes: "12:00:00" }, { opens: "13:00:00", closes: "15:30:00" }],
    "2026-12-24": [],
  } } } };

/** Every field the backend's PolicyBody accepts; it rejects any other (extra="forbid"). */
const POLICY_BODY_FIELDS = ["booking_horizon_days", "closing_buffer_minutes", "date_exceptions",
  "hold_minutes", "holiday_calendar", "maximum_buffer_minutes", "maximum_visit_minutes",
  "minimum_visit_gap_minutes", "opening_buffer_minutes", "slot_increment_minutes", "timezone",
  "weekly_windows"];

function setup(route: Route) {
  const notify = vi.fn();
  const { api, calls } = fakeApi(route);
  const request = vi.spyOn(api, "request");
  render(<OwnerProvider api={api} notify={notify}><SettingsTab /></OwnerProvider>);
  return { notify, calls, request };
}

const exceptionForm = () => screen.getByRole("form", { name: "Date exception" });

async function saveException(date: string, mode: string, opens?: string, closes?: string) {
  const form = within(exceptionForm());
  fireEvent.change(form.getByLabelText("Date"), { target: { value: date } });
  await userEvent.selectOptions(form.getByLabelText("Hours"), mode);
  if (opens) fireEvent.change(form.getByLabelText("Open"), { target: { value: opens } });
  if (closes) fireEvent.change(form.getByLabelText("Close"), { target: { value: closes } });
  await userEvent.click(screen.getByRole("button", { name: "Save exception" }));
}

function policyWrite(route: (body: unknown) => void, current: PolicyState = WITH_EXCEPTIONS): Route {
  return (method, path, body) => {
    if (path !== "/policy") return undefined;
    if (method === "PUT") { route(body); return { status: 200, body: current }; }
    return { status: 200, body: current };
  };
}

describe("SettingsTab", () => {
  it("offers to load the pilot policy when none is configured, then shows it", async () => {
    let seeded = false;
    const { notify, request } = setup((method, path) => {
      if (path === "/policy/seed" && method === "POST") { seeded = true; return { status: 200, body: {} }; }
      if (path === "/policy" && method === "GET") {
        return seeded ? { status: 200, body: POLICY }
          : { status: 404, body: { error: { code: "POLICY_NOT_CONFIGURED", message: "missing" } } };
      }
      return undefined;
    });
    expect(await screen.findByText("Policy is not configured")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load pilot policy" }));
    expect(await screen.findByText("Timezone: America/Los_Angeles")).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith("/policy/seed", expect.objectContaining({ method: "POST", idempotent: true }));
    expect(notify).toHaveBeenCalledWith("Pilot policy loaded", undefined);
    expect(screen.queryByRole("button", { name: "Load pilot policy" })).not.toBeInTheDocument();
    expect(exceptionForm()).toBeInTheDocument();
  });

  it("disables the seed button while the request is in flight", async () => {
    let releaseSeed: (() => void) | undefined;
    const pendingSeed = new Promise<void>((resolve) => { releaseSeed = resolve; });
    let seeded = false;
    setup(async (method, path) => {
      if (path === "/policy/seed" && method === "POST") {
        await pendingSeed;
        seeded = true;
        return { status: 200, body: {} };
      }
      if (path === "/policy") return seeded ? { status: 200, body: POLICY }
        : { status: 404, body: { error: { code: "POLICY_NOT_CONFIGURED", message: "missing" } } };
      return undefined;
    });
    const button = await screen.findByRole("button", { name: "Load pilot policy" });
    await userEvent.click(button);
    expect(button).toBeDisabled();
    releaseSeed?.();
    expect(await screen.findByText("Timezone: America/Los_Angeles")).toBeInTheDocument();
  });

  it("refreshes when another owner seeded the policy first", async () => {
    let seededElsewhere = false;
    const { notify } = setup((method, path) => {
      if (path === "/policy/seed" && method === "POST") {
        seededElsewhere = true;
        return { status: 409, body: { error: { code: "STALE_REVISION",
          message: "Calendar revision changed" } } };
      }
      if (path === "/policy") return seededElsewhere ? { status: 200, body: POLICY }
        : { status: 404, body: { error: { code: "POLICY_NOT_CONFIGURED", message: "missing" } } };
      return undefined;
    });
    await userEvent.click(await screen.findByRole("button", { name: "Load pilot policy" }));
    expect(await screen.findByText("Timezone: America/Los_Angeles")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load pilot policy" })).not.toBeInTheDocument();
    expect(exceptionForm()).toBeInTheDocument();
    expect(notify).toHaveBeenCalledWith(
      "Pilot policy could not be loaded because the policy or calendar changed. Current state has been refreshed.", true);
  });

  it("reports nothing saved when loading the pilot policy fails", async () => {
    const { notify } = setup((method, path) => {
      if (path === "/policy/seed") return { status: 500, body: { error: { code: "X", message: "boom" } } };
      if (path === "/policy") return { status: 404, body: { error: { code: "POLICY_NOT_CONFIGURED", message: "missing" } } };
      return undefined;
    });
    await userEvent.click(await screen.findByRole("button", { name: "Load pilot policy" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Nothing was saved: boom", true));
    expect(screen.getByRole("button", { name: "Load pilot policy" })).toBeEnabled();
  });

  it("does not offer to load the pilot policy once one exists", async () => {
    setup(() => undefined);
    await screen.findByText("Timezone: America/Los_Angeles");
    expect(screen.queryByRole("button", { name: "Load pilot policy" })).not.toBeInTheDocument();
  });

  it("summarizes the policy with date exceptions in date order", async () => {
    setup((method, path) => path === "/policy" ? { status: 200, body: WITH_EXCEPTIONS } : undefined);
    expect(await screen.findByText("Timezone: America/Los_Angeles")).toBeInTheDocument();
    expect(screen.getByText("Maximum visit: 180 minutes · Gap: 30 minutes")).toBeInTheDocument();
    expect(screen.getByText("Version 2 · calendar revision 4")).toBeInTheDocument();
    const lines = screen.getAllByText(/^2026-12-2/).map((item) => item.textContent);
    expect(lines).toEqual(["2026-12-24: Closed", "2026-12-26: 09:00–12:00, 13:00–15:30"]);
  });

  it("says when there are no date exceptions", async () => {
    setup(() => undefined);
    expect(await screen.findByText("No date exceptions")).toBeInTheDocument();
  });

  it("closes a date by sending the whole policy with an empty window list", async () => {
    const bodies: unknown[] = [];
    const { notify, request } = setup(policyWrite((body) => bodies.push(body)));
    await screen.findByText("Timezone: America/Los_Angeles");
    await saveException("2027-01-04", "closed");
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Date exception saved", undefined));
    expect(bodies).toEqual([{ expected_revision: 4, expected_version: 2, policy: {
      ...WITH_EXCEPTIONS.record.policy,
      date_exceptions: { ...WITH_EXCEPTIONS.record.policy.date_exceptions, "2027-01-04": [] } } }]);
    expect(request).toHaveBeenCalledWith("/policy", expect.objectContaining({ method: "PUT", idempotent: true }));
  });

  it("sets custom hours for a date", async () => {
    const bodies: { policy: { date_exceptions: unknown } }[] = [];
    setup(policyWrite((body) => bodies.push(body as { policy: { date_exceptions: unknown } })));
    await screen.findByText("Timezone: America/Los_Angeles");
    await saveException("2026-12-24", "custom", "10:00", "14:30");
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]?.policy.date_exceptions).toEqual({
      "2026-12-24": [{ opens: "10:00", closes: "14:30" }],
      "2026-12-26": WITH_EXCEPTIONS.record.policy.date_exceptions["2026-12-26"],
    });
  });

  it("restores normal hours by removing the date", async () => {
    const bodies: { policy: { date_exceptions: unknown } }[] = [];
    setup(policyWrite((body) => bodies.push(body as { policy: { date_exceptions: unknown } })));
    await screen.findByText("Timezone: America/Los_Angeles");
    await saveException("2026-12-26", "normal");
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]?.policy.date_exceptions).toEqual({ "2026-12-24": [] });
  });

  it("sends only fields the strict policy body accepts, in the GET serialization", async () => {
    const extra = { ...WITH_EXCEPTIONS, record: { ...WITH_EXCEPTIONS.record,
      policy: { ...WITH_EXCEPTIONS.record.policy, future_field: true } } };
    const bodies: { policy: Record<string, unknown> }[] = [];
    setup(policyWrite((body) => bodies.push(body as { policy: Record<string, unknown> }), extra));
    await screen.findByText("Timezone: America/Los_Angeles");
    await saveException("2027-01-04", "closed");
    await waitFor(() => expect(bodies).toHaveLength(1));
    const sent = bodies[0]?.policy ?? {};
    expect(Object.keys(sent).sort()).toEqual(POLICY_BODY_FIELDS);
    expect(sent.weekly_windows).toEqual({ "0": [{ opens: "08:00:00", closes: "17:00:00" }] });
    expect(sent.holiday_calendar).toBe("US_FEDERAL");
  });

  it("reports a conflict without keeping the edit", async () => {
    const { notify } = setup((method, path) => {
      if (path !== "/policy") return undefined;
      if (method === "PUT") return { status: 409, body: { error: { code: "STALE_REVISION",
        message: "Calendar changed" } } };
      return { status: 200, body: WITH_EXCEPTIONS };
    });
    await screen.findByText("Timezone: America/Los_Angeles");
    await saveException("2027-01-04", "closed");
    await waitFor(() => expect(notify).toHaveBeenCalledWith(
      "Nothing was saved: Calendar changed. Current state has been refreshed.", true));
    expect(screen.queryByText(/2027-01-04/)).not.toBeInTheDocument();
  });

  it("hides exception editing until a policy exists", async () => {
    const { calls } = setup((method, path) => path === "/policy"
      ? { status: 404, body: { error: { message: "Persist the pilot policy first" } } } : undefined);
    expect(await screen.findByText("Policy is not configured")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Date exception" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save exception" })).not.toBeInTheDocument();
    expect(calls.some((call) => call.method === "PUT")).toBe(false);
  });
});
