// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ApiError, type OwnerApi } from "@/lib/api";
import { OwnerProvider } from "@/owner/OwnerContext";

import { AdvancedPolicyForm } from "./AdvancedPolicyForm";
import { ExceptionForm } from "./ExceptionForm";
import { PolicySummary } from "./PolicySummary";
import { RegularPolicyForm } from "./RegularPolicyForm";
import { useOwner } from "@/owner/OwnerContext";

const WEEKDAY = [{ opens: "08:00:00", closes: "17:00:00" }];
const BASE = {
  timezone: "America/Los_Angeles", booking_horizon_days: 14, slot_increment_minutes: 15,
  maximum_visit_minutes: 180, minimum_visit_gap_minutes: 30, opening_buffer_minutes: 0,
  closing_buffer_minutes: 0, hold_minutes: 1440, maximum_buffer_minutes: 30,
  holiday_calendar: "US_FEDERAL", date_exceptions: {} as Record<string, unknown>,
  weekly_windows: Object.fromEntries([0, 1, 2, 3, 4].map((day) => [String(day), WEEKDAY])),
};
let policy: typeof BASE;
let version: number;
let revision: number;
let failPolicyLoad = false;
const request = vi.fn();
const api = {
  get: vi.fn(async (path: string) => {
    if (path === "/policy" && failPolicyLoad) throw new Error("offline");
    if (path === "/policy") return { record: { version, policy }, calendar_revision: revision };
    if (path === "/calendar") return { revision, events: [] };
    return [];
  }),
  request,
} as unknown as OwnerApi;

function Page() {
  const { loaded, data } = useOwner();
  return loaded ? <>
    <PolicySummary policy={data.policy} />
    <RegularPolicyForm /><ExceptionForm /><AdvancedPolicyForm />
  </> : null;
}

function setValue(element: HTMLInputElement | HTMLSelectElement, value: string) {
  const proto = element instanceof HTMLSelectElement ? HTMLSelectElement : HTMLInputElement;
  Object.getOwnPropertyDescriptor(proto.prototype, "value")!.set!.call(element, value);
  element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}

describe("Scheduling policy settings", () => {
  let host: HTMLDivElement;
  let root: Root;
  const field = (label: string) => {
    const item = [...host.querySelectorAll("label")].find((node) => node.textContent?.startsWith(label))!;
    return (item.htmlFor ? host.querySelector(`#${item.htmlFor}`) : item.querySelector("input,select"))! as HTMLInputElement;
  };
  const click = async (text: string) => act(async () => [...host.querySelectorAll<HTMLButtonElement>("button")]
    .find((button) => (button.getAttribute("aria-label") ?? button.textContent) === text)!.click());
  const alerts = () => [...host.querySelectorAll('[role="alert"]')].map((node) => node.textContent);

  beforeEach(async () => {
    policy = structuredClone(BASE); version = 1; revision = 7; failPolicyLoad = false;
    request.mockReset();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><Page /></OwnerProvider>));
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

  it("summarizes weekly hours, holiday behavior, horizon, and advanced limits", () => {
    const text = host.querySelector(".card")!.textContent!;
    expect(text).toContain("Monday: 08:00–17:00");
    expect(text).toContain("Saturday: Closed");
    expect(text).toContain("closed on observed US federal holidays");
    expect(text).toContain("Booking horizon: 14 days ahead");
    expect(text).toContain("Hold: 1440 minutes");
    expect(text).toContain("Version 1 · calendar revision 7");
  });

  it("saves an ordinary edit against the loaded version and revision, then shows it", async () => {
    request.mockImplementation(async (_path: string, options: { body: { policy: typeof BASE } }) => {
      policy = { ...policy, ...options.body.policy } as typeof BASE; version = 2; revision = 8;
    });
    await act(async () => setValue(field("Booking horizon"), "30"));
    expect(host.textContent).toContain("unsaved changes");
    await click("Save regular settings");
    const [path, options] = request.mock.calls[0]!;
    expect(path).toBe("/policy");
    expect(options.method).toBe("PUT");
    expect(options.idempotencyKey).toEqual(expect.any(String));
    expect(options.body).toMatchObject({ expected_revision: 7, expected_version: 1,
      policy: { booking_horizon_days: 30, holiday_calendar: "US_FEDERAL", timezone: "America/Los_Angeles",
        hold_minutes: 1440 } });
    expect(host.querySelector(".card")!.textContent).toContain("Booking horizon: 30 days ahead");
    expect(host.querySelector(".card")!.textContent).toContain("Version 2 · calendar revision 8");
    expect(host.textContent).not.toContain("unsaved changes");
  });

  it("edits weekly hours, adding a Saturday window and closing Friday", async () => {
    request.mockResolvedValue(undefined);
    await click("Add Saturday hours");
    await act(async () => setValue(field("Saturday closes"), "12:00"));
    await click("Remove Friday hours 1");
    await click("Save regular settings");
    const weekly = request.mock.calls[0]![1].body.policy.weekly_windows;
    expect(weekly["5"]).toEqual([{ opens: "08:00", closes: "12:00" }]);
    expect(weekly["4"]).toBeUndefined();
    expect(weekly["0"]).toEqual([{ opens: "08:00", closes: "17:00" }]);
  });

  it("blocks invalid hours with one summary alert and field-level errors", async () => {
    await act(async () => setValue(field("Monday closes"), "07:00"));
    await act(async () => setValue(field("Booking horizon"), "0"));
    await click("Save regular settings");
    expect(request).not.toHaveBeenCalled();
    expect(alerts().filter((text) => text?.startsWith("Fix"))).toEqual(["Fix 2 fields before saving."]);
    const horizon = field("Booking horizon");
    expect(horizon.getAttribute("aria-invalid")).toBe("true");
    expect(host.querySelector(`#${horizon.getAttribute("aria-describedby")!.split(" ")[0]}`)!.textContent)
      .toContain("whole number of days");
    expect(field("Monday closes").getAttribute("aria-invalid")).toBe("true");
  });

  it("explains a conflicting closure and keeps the saved policy and the typed input", async () => {
    request.mockRejectedValueOnce(new ApiError("Policy edit would invalidate an active reservation", 409,
      "SLOT_CONFLICT", undefined));
    const date = field("Date");
    await act(async () => setValue(date, "2026-10-14"));
    await act(async () => host.querySelector<HTMLFormElement>("form[aria-label='Date exception']")!.requestSubmit());
    expect(alerts().join(" ")).toContain("conflicts with an active booking or pending request");
    expect(alerts().join(" ")).toContain("Your saved policy is unchanged");
    expect(field("Date").value).toBe("2026-10-14");
    expect(host.querySelector(".card")!.textContent).toContain("No date exceptions");
    expect(host.querySelector(".card")!.textContent).toContain("Version 1");
  });

  it("keeps edits on a stale version, shows the newer policy, and retries with the new version", async () => {
    await act(async () => setValue(field("Booking horizon"), "21"));
    request.mockImplementationOnce(async () => {
      policy = { ...policy, hold_minutes: 600 }; version = 2; revision = 8; // someone else saved
      throw new ApiError("Policy version changed", 409, "STALE_VERSION", undefined);
    });
    await click("Save regular settings");
    expect(alerts().join(" ")).toContain("changed since you opened it");
    expect(host.querySelector(".card")!.textContent).toContain("Version 2 · calendar revision 8");
    expect(host.querySelector(".card")!.textContent).toContain("Hold: 600 minutes");
    expect(field("Booking horizon").value).toBe("21");
    request.mockResolvedValueOnce(undefined);
    await click("Save regular settings");
    const [, options] = request.mock.calls[1]!;
    expect(options.body).toMatchObject({ expected_version: 2, expected_revision: 8,
      policy: { booking_horizon_days: 21, hold_minutes: 600 } });
    expect(options.idempotencyKey).not.toBe(request.mock.calls[0]![1].idempotencyKey);
  });

  it("opens a federal holiday with a date exception and with the holiday setting", async () => {
    request.mockResolvedValue(undefined);
    await act(async () => setValue(field("Holidays"), "none"));
    await click("Save regular settings");
    expect(request.mock.calls[0]![1].body.policy.holiday_calendar).toBeNull();

    const form = host.querySelector<HTMLFormElement>("form[aria-label='Date exception']")!;
    const set = (name: string, value: string) => setValue(form.elements.namedItem(name) as HTMLInputElement, value);
    await act(async () => { set("date", "2026-11-11"); set("mode", "custom"); set("opens", "09:00"); set("closes", "12:00"); });
    await act(async () => form.requestSubmit());
    expect(request.mock.calls[1]![1].body.policy.date_exceptions).toEqual({
      "2026-11-11": [{ opens: "09:00", closes: "12:00" }] });
  });

  it("validates advanced limits and warns about the timezone before saving it", async () => {
    expect(host.textContent).toContain("Existing visits keep their exact moment in time");
    await act(async () => setValue(field("Gap between visits"), "45"));
    await click("Save advanced settings");
    expect(request).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Maximum buffer must be at least the gap between visits.");
    await act(async () => setValue(field("Maximum buffer"), "45"));
    await act(async () => setValue(field("Business timezone"), "America/New_York"));
    request.mockRejectedValueOnce(new ApiError("The owner API could not be reached", 0, null, undefined));
    await click("Save advanced settings");
    request.mockResolvedValueOnce(undefined);
    await click("Save advanced settings");
    expect(request.mock.calls[1]![1].body.policy).toMatchObject({ timezone: "America/New_York",
      minimum_visit_gap_minutes: 45, maximum_buffer_minutes: 45 });
    expect(request.mock.calls[1]![1].idempotencyKey).toBe(request.mock.calls[0]![1].idempotencyKey);
  });

  it("rejects an unknown timezone", async () => {
    await act(async () => setValue(field("Business timezone"), "Mars/Olympus"));
    await click("Save advanced settings");
    expect(request).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Enter a valid timezone name");
  });

  const elsewhere = (change: Partial<typeof BASE>) => {
    policy = { ...policy, ...change }; version = 2; revision = 8;
    throw new ApiError("Policy version changed", 409, "STALE_VERSION", undefined);
  };

  it("does not revert another session's regular changes after a stale save", async () => {
    await act(async () => setValue(field("Booking horizon"), "21"));
    request.mockImplementationOnce(async () => elsewhere({ holiday_calendar: null as never,
      weekly_windows: { ...policy.weekly_windows, "5": [{ opens: "09:00:00", closes: "12:00:00" }] } }));
    await click("Save regular settings");
    expect(field("Booking horizon").value).toBe("21");
    expect(field("Saturday opens").value).toBe("09:00");
    expect(field("Holidays").value).toBe("none");
    expect(host.textContent).not.toContain("Also changed elsewhere");
    request.mockResolvedValueOnce(undefined);
    await click("Save regular settings");
    const sent = request.mock.calls[1]![1].body;
    expect(sent.expected_version).toBe(2);
    expect(sent.policy.weekly_windows["5"]).toEqual([{ opens: "09:00", closes: "12:00" }]);
    expect(sent.policy.holiday_calendar).toBeNull();
    expect(sent.policy.booking_horizon_days).toBe(21);
  });

  it("flags a regular field changed both here and elsewhere and keeps the owner's value", async () => {
    await act(async () => setValue(field("Monday closes"), "16:00"));
    request.mockImplementationOnce(async () => elsewhere({
      weekly_windows: { ...policy.weekly_windows, "0": [{ opens: "08:00:00", closes: "15:00:00" }] } }));
    await click("Save regular settings");
    expect(field("Monday closes").value).toBe("16:00");
    expect(host.textContent).toContain("Also changed elsewhere, still showing your value: Monday hours");
  });

  it("does not revert another session's advanced changes, including the timezone", async () => {
    await act(async () => setValue(field("Hold duration"), "600"));
    request.mockImplementationOnce(async () => elsewhere({ timezone: "America/Denver",
      maximum_visit_minutes: 240, slot_increment_minutes: 30 }));
    await click("Save advanced settings");
    expect(field("Hold duration").value).toBe("600");
    expect(field("Business timezone").value).toBe("America/Denver");
    expect(field("Maximum visit length").value).toBe("240");
    request.mockResolvedValueOnce(undefined);
    await click("Save advanced settings");
    expect(request.mock.calls[1]![1].body).toMatchObject({ expected_version: 2, policy: {
      timezone: "America/Denver", maximum_visit_minutes: 240, slot_increment_minutes: 30, hold_minutes: 600 } });
  });

  it("flags an advanced field changed on both sides", async () => {
    await act(async () => setValue(field("Hold duration"), "600"));
    request.mockImplementationOnce(async () => elsewhere({ hold_minutes: 720 }));
    await click("Save advanced settings");
    expect(field("Hold duration").value).toBe("600");
    expect(host.textContent).toContain("still showing your value: Hold duration");
  });

  it("does not claim the newer policy loaded when the reload fails", async () => {
    await act(async () => setValue(field("Booking horizon"), "21"));
    request.mockImplementationOnce(async () => { failPolicyLoad = true; throw new ApiError("Policy version changed", 409, "STALE_VERSION", undefined); });
    await click("Save regular settings");
    expect(host.textContent).toContain("newer policy could not be loaded");
    expect(host.textContent).not.toContain("now show the newer saved values");
  });

  it("treats a draft the server normalizes as saved", async () => {
    request.mockImplementation(async (_path: string, options: { body: { policy: typeof BASE } }) => {
      policy = { ...policy, ...options.body.policy } as typeof BASE; version = 2; revision = 8;
    });
    await act(async () => setValue(field("Booking horizon"), "021"));
    await click("Save regular settings");
    expect(field("Booking horizon").value).toBe("21");
    await act(async () => setValue(field("Business timezone"), "  America/Denver "));
    await click("Save advanced settings");
    expect(field("Business timezone").value).toBe("America/Denver");
    expect(host.textContent).not.toContain("unsaved changes");
  });

  /** A save whose response the test releases later, so the owner can type while it is in flight. */
  function pending() {
    let finish!: () => void;
    request.mockImplementationOnce((_path: string, options: { body: { policy: typeof BASE } }) =>
      new Promise<void>((resolve) => { finish = () => {
        policy = { ...policy, ...options.body.policy } as typeof BASE; version += 1; revision += 1; resolve();
      }; }));
    return () => act(async () => finish());
  }

  it("keeps regular text typed during a save, even when it returns to the old value", async () => {
    for (const [typed, expected] of [["14", "14"], ["210", "210"]] as const) {
      policy = structuredClone(BASE); version = 1; revision = 7;
      await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><Page /></OwnerProvider>));
      await act(async () => setValue(field("Booking horizon"), "21"));
      const release = pending();
      await click("Save regular settings");
      await act(async () => setValue(field("Booking horizon"), typed));
      await release();
      expect(field("Booking horizon").value).toBe(expected);
      expect(host.textContent).not.toContain("Also changed elsewhere");
      expect(host.textContent).toContain("Booking horizon: 21 days ahead");
      expect(host.textContent).toContain("unsaved changes");
      await act(async () => root.render(null));
    }
  });

  it("keeps advanced text typed during a save, even when it returns to the old value", async () => {
    for (const typed of ["1440", "6000"]) {
      policy = structuredClone(BASE); version = 1; revision = 7;
      await act(async () => root.render(<OwnerProvider api={api} notify={() => undefined}><Page /></OwnerProvider>));
      await act(async () => setValue(field("Hold duration"), "600"));
      const release = pending();
      await click("Save advanced settings");
      await act(async () => setValue(field("Hold duration"), typed));
      await release();
      expect(field("Hold duration").value).toBe(typed);
      expect(host.textContent).not.toContain("Also changed elsewhere");
      expect(host.textContent).toContain("Hold: 600 minutes");
      expect(host.textContent).toContain("unsaved changes");
      await act(async () => root.render(null));
    }
  });

  it("clears an 'also changed elsewhere' flag once the owner's value is saved", async () => {
    await act(async () => setValue(field("Hold duration"), "600"));
    request.mockImplementationOnce(async () => elsewhere({ hold_minutes: 720 }));
    await click("Save advanced settings");
    expect(host.textContent).toContain("still showing your value: Hold duration");
    request.mockImplementation(async (_path: string, options: { body: { policy: typeof BASE } }) => {
      policy = { ...policy, ...options.body.policy } as typeof BASE; version += 1; revision += 1;
    });
    await click("Save advanced settings");
    expect(host.textContent).not.toContain("Also changed elsewhere");
    expect(host.textContent).not.toContain("unsaved changes");
  });

  it("merges again after a second stale rejection", async () => {
    await act(async () => setValue(field("Booking horizon"), "21"));
    request.mockImplementationOnce(async () => elsewhere({ holiday_calendar: null as never }));
    await click("Save regular settings");
    request.mockImplementationOnce(async () => {
      policy = { ...policy, weekly_windows: { ...policy.weekly_windows,
        "5": [{ opens: "09:00:00", closes: "12:00:00" }] } }; version = 3; revision = 9;
      throw new ApiError("Policy version changed", 409, "STALE_VERSION", undefined);
    });
    await click("Save regular settings");
    expect(field("Saturday opens").value).toBe("09:00");
    expect(field("Holidays").value).toBe("none");
    expect(field("Booking horizon").value).toBe("21");
    request.mockResolvedValueOnce(undefined);
    await click("Save regular settings");
    expect(request.mock.calls[2]![1].body).toMatchObject({ expected_version: 3, expected_revision: 9,
      policy: { booking_horizon_days: 21, holiday_calendar: null } });
    expect(request.mock.calls[2]![1].body.policy.weekly_windows["5"]).toEqual([{ opens: "09:00", closes: "12:00" }]);
  });

  it("shows the newer value and clears the flag when the owner discards after a merge", async () => {
    await act(async () => setValue(field("Monday closes"), "16:00"));
    request.mockImplementationOnce(async () => elsewhere({
      weekly_windows: { ...policy.weekly_windows, "0": [{ opens: "08:00:00", closes: "15:00:00" }] } }));
    await click("Save regular settings");
    expect(host.textContent).toContain("Also changed elsewhere");
    await click("Discard my edits");
    expect(field("Monday closes").value).toBe("15:00");
    expect(host.textContent).not.toContain("Also changed elsewhere");
    expect(host.textContent).not.toContain("unsaved changes");
  });
});
