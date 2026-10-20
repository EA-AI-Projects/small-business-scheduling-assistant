// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ApiError, type OwnerApi } from "@/lib/api";
import { OwnerProvider } from "@/owner/OwnerContext";

import { SettingsTab } from "./SettingsTab";

const POLICY = { timezone: "America/Los_Angeles", booking_horizon_days: 14,
  weekly_windows: { "0": [{ opens: "08:00:00", closes: "17:00:00" }] },
  date_exceptions: {}, holiday_calendar: "US_FEDERAL", slot_increment_minutes: 15,
  maximum_visit_minutes: 180, maximum_buffer_minutes: 30, minimum_visit_gap_minutes: 30,
  opening_buffer_minutes: 0, closing_buffer_minutes: 0, hold_minutes: 1440 };
type Rule = { weekdays: number[]; windows: { start: string; end: string }[]; client_id?: string };
const MORNINGS: Rule[] = [{ weekdays: [1, 3], windows: [{ start: "08:00", end: "12:00" }] }];
const ALDER = { weekdays: [3], windows: [{ start: "13:00", end: "16:00" }], client_id: "client-1" };
const READY = { ready: true, timezone: "America/Los_Angeles", rules: MORNINGS,
  clients: [], ambiguities: [], review_token: "a".repeat(64), problems: [] };
let preferences: { version: number; active: null | { text: string; rules: Rule[]; timezone: string;
  clients?: { client_id: string; name: string | null }[]; review_token: string } };
const get = vi.fn(async (path: string) => {
  if (path === "/scheduling-preferences") return preferences;
  if (path === "/policy") return { record: { policy: POLICY, version: 1 }, calendar_revision: 1 };
  if (path === "/profile") return { profile: null, version: 0 };
  if (path === "/booking-outreach") return { version: 1, settings: { enabled: false } };
  if (path === "/calendar") return { revision: 1, events: [] };
  return [];
});
const request = vi.fn();
const api = { get, request } as unknown as OwnerApi;

function setText(element: HTMLTextAreaElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("owner Scheduling preferences", () => {
  let host: HTMLDivElement;
  let root: Root;
  const opener = () => host.querySelector<HTMLButtonElement>('button[aria-label="Edit Scheduling preferences"]')!;
  const text = () => document.querySelector<HTMLTextAreaElement>("#scheduling-preferences-text")!;
  const press = (label: string) => act(async () => [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find((button) => button.textContent === label)!.click());
  const button = (label: string) => [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find((candidate) => candidate.textContent === label);

  beforeEach(async () => {
    preferences = { version: 0, active: null };
    request.mockReset();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><SettingsTab /></OwnerProvider>));
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });

  it("adds a fifth card that summarizes active preferences", async () => {
    expect(host.querySelectorAll('button[aria-haspopup="dialog"]')).toHaveLength(5);
    expect(opener().textContent).toContain("None active");
    preferences = { version: 2, active: { text: "Tuesday and Thursday mornings", rules: MORNINGS,
      timezone: "America/Los_Angeles", review_token: "a".repeat(64) } };
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined} key="again"><SettingsTab /></OwnerProvider>));
    expect(opener().textContent).toContain("Tuesday, Thursday: visits start 8:00 AM–12:00 PM");
  });

  it("reviews with exact days and ranges, and activates only the reviewed text", async () => {
    await act(async () => opener().click());
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    await act(async () => setText(text(), "Tuesday and Thursday mornings"));
    request.mockResolvedValueOnce(READY);
    await press("Review changes");
    expect(request).toHaveBeenCalledWith("/scheduling-preferences/review",
      { method: "POST", body: { text: "Tuesday and Thursday mornings" } });
    const result = document.querySelector<HTMLElement>(".preference-review")!;
    expect(result.textContent).toContain("Tuesday, Thursday: visits start 8:00 AM–12:00 PM");
    expect(result.textContent).toContain("America/Los_Angeles");
    expect(opener().textContent).toContain("None active"); // reviewing activates nothing
    expect(request).toHaveBeenCalledTimes(1);

    request.mockResolvedValueOnce({ version: 1, active: { text: "Tuesday and Thursday mornings",
      rules: MORNINGS, timezone: "America/Los_Angeles", review_token: READY.review_token } });
    await press("Activate preferences");
    expect(request).toHaveBeenLastCalledWith("/scheduling-preferences", expect.objectContaining({
      method: "PUT", body: { review_token: READY.review_token, expected_version: 0 } }));
    expect(opener().textContent).toContain("Active · Tuesday, Thursday");
    expect(document.querySelector('[aria-label="Active preferences"]')!.textContent)
      .toContain("Tuesday and Thursday mornings");
    expect(button("Activate preferences")).toBeUndefined();
  });

  it("reviews client guidance by current profile name and shows it in the active summary", async () => {
    await act(async () => opener().click());
    await act(async () => setText(text(), "Mornings, and Client Alder on Thursday afternoons"));
    request.mockResolvedValueOnce({ ...READY, rules: [...MORNINGS, ALDER],
      clients: [{ client_id: "client-1", name: "Client Alder" }] });
    await press("Review changes");
    const result = document.querySelector<HTMLElement>(".preference-review")!;
    expect(result.textContent).toContain("Applies to: Client Alder");
    expect(result.textContent).toContain("Client Alder — Thursday: visits start 1:00 PM–4:00 PM");
    expect(result.textContent).toContain("Tuesday, Thursday: visits start 8:00 AM–12:00 PM");
    request.mockResolvedValueOnce({ version: 1, active: { text: "x", rules: [...MORNINGS, ALDER],
      clients: [{ client_id: "client-1", name: "Client Alder" }], timezone: "America/Los_Angeles",
      review_token: READY.review_token } });
    await press("Activate preferences");
    expect(opener().textContent).toContain("Client Alder — Thursday: visits start 1:00 PM–4:00 PM");
  });

  const CEDARS = [
    { client_id: "c1", name: "Cedar Household", phone_last4: "0003", service_address: "1 Test Street" },
    { client_id: "c2", name: "Cedar Household", phone_last4: "0004", service_address: "2 Test Street" }];
  const ambiguous = { ready: false, timezone: "America/Los_Angeles", rules: [], clients: [], review_token: null,
    problems: [{ kind: "ambiguous_client", message: "\"Cedar Household\" may mean more than one client." }],
    ambiguities: [{ name: "Cedar Household", candidates: CEDARS }] };
  const readyFor = (id: string) => ({ ...READY, rules: [{ weekdays: [4], windows: [], client_id: id }],
    clients: [{ ...CEDARS.find((c) => c.client_id === id)!, shared_name: true }] });

  it("keeps the picker while picks change, then re-reviews and shows which profile is bound", async () => {
    await act(async () => opener().click());
    await act(async () => setText(text(), "Cedar Household prefers Fridays"));
    request.mockResolvedValueOnce(ambiguous);
    await press("Review changes");
    expect(button("Activate preferences")).toBeUndefined();
    const radio = (id: string) => document.querySelector<HTMLInputElement>(`input[type="radio"][value="${id}"]`)!;
    expect(document.querySelector("fieldset legend")!.textContent).toContain("Cedar Household");
    expect(document.querySelector(`label[for="${radio("c2").id}"]`)!.textContent).toContain("phone ending 0004");

    const first = radio("c1");
    await act(async () => first.click());
    // The picker stays mounted (same node), Activate stays hidden, and the message is about the selection.
    expect(radio("c1")).toBe(first);
    expect(first.checked).toBe(true);
    expect(document.querySelector(".preference-review")!.textContent).toContain("Review again to apply your selection");
    expect(document.querySelector(".preference-review")!.textContent).not.toContain("The text changed");
    await act(async () => radio("c2").click());
    expect(radio("c1")).toBe(first);
    expect(button("Activate preferences")).toBeUndefined();

    request.mockResolvedValueOnce(readyFor("c2"));
    await press("Review changes");
    expect(request).toHaveBeenLastCalledWith("/scheduling-preferences/review", { method: "POST",
      body: { text: "Cedar Household prefers Fridays", client_selections: [{ name: "Cedar Household", client_id: "c2" }] } });
    const result = document.querySelector<HTMLElement>(".preference-review")!;
    expect(result.textContent).toContain("Cedar Household (phone ending 0004, 2 Test Street)");
    expect(result.textContent).not.toContain("0003");
    expect(button("Activate preferences")).toBeDefined();
  });

  it("shows every ambiguous name's picker at once", async () => {
    await act(async () => opener().click());
    await act(async () => setText(text(), "Cedar Household Fridays, Pine Household Mondays"));
    request.mockResolvedValueOnce({ ...ambiguous, ambiguities: [...ambiguous.ambiguities,
      { name: "Pine Household", candidates: [{ ...CEDARS[0]!, client_id: "p1", name: "Pine Household" },
        { ...CEDARS[1]!, client_id: "p2", name: "Pine Household" }] }] });
    await press("Review changes");
    expect(document.querySelectorAll("fieldset.client-picker")).toHaveLength(2);
  });

  it("refuses a rejected selection, clears the picks, and keeps the draft", async () => {
    await act(async () => opener().click());
    await act(async () => setText(text(), "Cedar Household prefers Fridays"));
    request.mockResolvedValueOnce(ambiguous);
    await press("Review changes");
    await act(async () => document.querySelector<HTMLInputElement>('input[type="radio"][value="c1"]')!.click());
    request.mockRejectedValueOnce(new ApiError("Choose a client from this business", 422,
      "INVALID_CLIENT_SELECTION", undefined));
    await press("Review changes");
    expect(document.querySelector('.preference-review [role="alert"]')!.textContent)
      .toContain("Choose a client from this business");
    expect(button("Activate preferences")).toBeUndefined();
    expect(text().value).toBe("Cedar Household prefers Fridays");
    expect(opener().textContent).toContain("None active");
    // The next review starts without the rejected pick.
    request.mockResolvedValueOnce(ambiguous);
    await press("Review changes");
    expect(request).toHaveBeenLastCalledWith("/scheduling-preferences/review",
      { method: "POST", body: { text: "Cedar Household prefers Fridays" } });
    expect(document.querySelector<HTMLInputElement>('input[type="radio"][value="c1"]')!.checked).toBe(false);
  });

  it("explains an unknown client and keeps Activate hidden", async () => {
    await act(async () => opener().click());
    await act(async () => setText(text(), "Client Zed prefers Mondays"));
    request.mockResolvedValueOnce({ ready: false, timezone: "America/Los_Angeles", rules: [], clients: [],
      ambiguities: [], review_token: null,
      problems: [{ kind: "unknown_client", message: "No client named \"Client Zed\" was found." }] });
    await press("Review changes");
    const alert = document.querySelector<HTMLElement>('.preference-review [role="alert"]')!;
    expect(alert.textContent).toContain("Client not found");
    expect(button("Activate preferences")).toBeUndefined();
  });

  it("blocks activation and explains what to correct", async () => {
    await act(async () => opener().click());
    await act(async () => setText(text(), "Group nearby clients"));
    request.mockResolvedValueOnce({ ready: false, timezone: "America/Los_Angeles", rules: [],
      clients: [], ambiguities: [], review_token: null, problems: [{ kind: "unsupported", message: "Route grouping is not supported." }] });
    await press("Review changes");
    const alert = document.querySelector<HTMLElement>('.preference-review [role="alert"]')!;
    expect(alert.textContent).toContain("Not supported");
    expect(alert.textContent).toContain("Route grouping is not supported.");
    expect(button("Activate preferences")).toBeUndefined();
    expect(document.activeElement).toBe(document.querySelector(".preference-review"));
    expect(opener().textContent).toContain("None active");
  });

  it("requires a new review after the text changes", async () => {
    await act(async () => opener().click());
    await act(async () => setText(text(), "Tuesday mornings"));
    request.mockResolvedValueOnce(READY);
    await press("Review changes");
    expect(button("Activate preferences")).toBeDefined();
    await act(async () => setText(text(), "Tuesday mornings and Friday"));
    expect(button("Activate preferences")).toBeUndefined();
    expect(document.body.textContent).toContain("Review changes again before activating");
    await act(async () => setText(text(), "Tuesday mornings"));
    expect(button("Activate preferences")).toBeDefined(); // same text as the review covered
  });

  it("keeps the draft and the active preferences when review or activation fails, and retries safely", async () => {
    await act(async () => opener().click());
    await act(async () => setText(text(), "Tuesday mornings"));
    request.mockRejectedValueOnce(new ApiError("The assistant could not review these preferences. Try again.", 503,
      "INTERPRETER_UNAVAILABLE", undefined));
    await press("Review changes");
    expect(document.querySelector('.preference-review [role="alert"]')!.textContent).toContain("Nothing was reviewed or changed");
    expect(text().value).toBe("Tuesday mornings");

    request.mockResolvedValueOnce(READY);
    await press("Review changes");
    request.mockRejectedValueOnce(new ApiError("The owner API could not be reached", 0, null, undefined));
    await press("Activate preferences");
    expect(document.body.textContent).toContain("Nothing was activated");
    expect(opener().textContent).toContain("None active");
    request.mockResolvedValueOnce({ version: 1, active: { text: "Tuesday mornings", rules: MORNINGS,
      timezone: "America/Los_Angeles", review_token: READY.review_token } });
    await press("Activate preferences");
    const keys = request.mock.calls.filter(([path]) => path === "/scheduling-preferences")
      .map((call) => call[1].idempotencyKey);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it("keeps the draft after a stale activation and shows the newer active version", async () => {
    await act(async () => opener().click());
    await act(async () => setText(text(), "Tuesday mornings"));
    request.mockResolvedValueOnce(READY);
    await press("Review changes");
    request.mockRejectedValueOnce(new ApiError("stale", 409, "STALE_VERSION", { version: 3, active: {
      text: "Newer text", rules: MORNINGS, timezone: "America/Los_Angeles", review_token: "b".repeat(64) } }));
    await press("Activate preferences");
    expect(text().value).toBe("Tuesday mornings");
    expect(document.body.textContent).toContain("changed elsewhere (now version 3)");
    expect(document.querySelector('[aria-label="Active preferences"]')!.textContent).toContain("Newer text");
  });

  it("clears active preferences explicitly", async () => {
    preferences = { version: 4, active: { text: "Tuesday mornings", rules: MORNINGS,
      timezone: "America/Los_Angeles", review_token: READY.review_token } };
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined} key="x"><SettingsTab /></OwnerProvider>));
    await act(async () => opener().click());
    request.mockResolvedValueOnce({ version: 5, active: null });
    await press("Clear preferences");
    expect(request).toHaveBeenLastCalledWith("/scheduling-preferences", expect.objectContaining({
      method: "DELETE", body: { expected_version: 4 } }));
    expect(opener().textContent).toContain("None active");
    expect(text().value).toBe("");
  });

  it("asks before discarding an unactivated draft and uses the phone sheet when narrow", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
    await act(async () => opener().click());
    expect(document.querySelector('[role="dialog"]')?.classList.contains("sheet")).toBe(true);
    await act(async () => setText(text(), "Friday afternoons"));
    expect(opener().textContent).toContain("Unsaved changes");
    await act(async () => document.querySelector<HTMLButtonElement>(".popover-close")!.click());
    expect(document.querySelector('[role="alertdialog"]')).not.toBeNull();
    expect(document.activeElement?.textContent).toBe("Keep editing");
    await press("Keep editing");
    expect(text().value).toBe("Friday afternoons");
    await act(async () => document.querySelector<HTMLButtonElement>(".popover-close")!.click());
    await press("Discard changes");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(opener().textContent).not.toContain("Unsaved changes");
  });

  it("shows an accessible error with Retry when the preferences cannot load", async () => {
    let failing = true;
    get.mockImplementation(async (path: string) => {
      if (path === "/scheduling-preferences" && failing) throw new Error("offline");
      if (path === "/scheduling-preferences") return preferences;
      if (path === "/policy") return { record: { policy: POLICY, version: 1 }, calendar_revision: 1 };
      if (path === "/profile") return { profile: null, version: 0 };
      if (path === "/booking-outreach") return { version: 1, settings: { enabled: false } };
      if (path === "/calendar") return { revision: 1, events: [] };
      return [];
    });
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined} key="fail"><SettingsTab /></OwnerProvider>));
    expect(opener().textContent).toContain("could not load");
    await act(async () => opener().click());
    const alert = document.querySelector('[role="dialog"] [role="alert"]')!;
    expect(alert.textContent).toContain("could not load");
    expect(document.body.textContent).not.toContain("Loading scheduling preferences");
    failing = false;
    await press("Retry");
    expect(text()).not.toBeNull();
    expect(document.querySelector('[role="dialog"] [role="alert"]')).toBeNull();
  });
});
