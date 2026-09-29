import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { fakeApi, type Route } from "@/test/fakeApi";

import { OwnerProvider, useOwner } from "./OwnerContext";

function Probe() {
  const { data, loaded, stamp, change } = useOwner();
  return (
    <div>
      <span>{loaded ? `zone ${data.zone} v${stamp.version} keep=${String(stamp.preserveSelection)}` : "loading"}</span>
      <button type="button" onClick={() => void change("/blocks", "POST", { a: 1 }, "Saved")}>write</button>
    </div>
  );
}

function setup(route: Route) {
  const notify = vi.fn();
  const { api, calls } = fakeApi(route);
  render(<OwnerProvider api={api} notify={notify}><Probe /></OwnerProvider>);
  return { notify, calls };
}

describe("OwnerProvider", () => {
  it("loads calendar, requests, clients and policy in the business timezone", async () => {
    setup(() => undefined);
    expect(await screen.findByText("zone America/Los_Angeles v1 keep=false")).toBeInTheDocument();
  });

  it("treats a missing policy as unconfigured rather than an error", async () => {
    const { notify } = setup((method, path) =>
      path === "/policy" ? { status: 404, body: { error: { message: "Persist the pilot policy first" } } } : undefined);
    expect(await screen.findByText("zone UTC v1 keep=false")).toBeInTheDocument();
    expect(notify).not.toHaveBeenCalled();
  });

  it("refreshes and confirms after a committed write", async () => {
    const { notify, calls } = setup((method) => method === "POST" ? { status: 200, body: {} } : undefined);
    await screen.findByText(/v1/);
    await userEvent.click(screen.getByText("write"));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Saved", undefined));
    expect(screen.getByText(/v2 keep=false/)).toBeInTheDocument();
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
  });

  it("on conflict says nothing was saved and reloads while keeping the selection", async () => {
    const { notify } = setup((method) => method === "POST"
      ? { status: 409, body: { error: { code: "STALE_VERSION", message: "Calendar changed" } } } : undefined);
    await screen.findByText(/v1/);
    await userEvent.click(screen.getByText("write"));
    await waitFor(() => expect(notify).toHaveBeenCalledWith(
      "Nothing was saved: Calendar changed. Current state has been refreshed.", true));
    expect(screen.getByText(/v2 keep=true/)).toBeInTheDocument();
  });

  it("reports other failures without refreshing", async () => {
    const { notify } = setup((method) => method === "POST"
      ? { status: 422, body: { error: { message: "Invalid" } } } : undefined);
    await screen.findByText(/v1/);
    await userEvent.click(screen.getByText("write"));
    await waitFor(() => expect(notify).toHaveBeenCalledWith("Nothing was saved: Invalid", true));
    expect(screen.getByText(/v1/)).toBeInTheDocument();
  });
});
