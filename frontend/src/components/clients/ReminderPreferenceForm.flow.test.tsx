// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ApiError } from "@/lib/api";
import type { ReminderPreference } from "@/lib/reminders";
import { ReminderPreferenceForm } from "./ReminderPreferenceForm";

const initial: ReminderPreference = { enabled: false, version: 3, days_before: 2, local_time: "09:15:00",
  timezone: "America/Los_Angeles", blockers: ['phone_unverified', 'consent_missing', 'sms_opted_out'],
  upcoming_reminders: [{ appointment_id: "visit", start_at: "2026-12-02T18:00:00Z",
    run_at: "2026-11-30T17:15:00Z", status: "disabled" }] };
let saved = initial;
const get = vi.fn(async () => saved);
const request = vi.fn();
const refresh = vi.fn();
const api = { get, request };
vi.mock("@/owner/OwnerContext", () => ({ useOwner: () => ({ api, refresh }) }));

let host: HTMLDivElement;
let root: Root;
beforeEach(async () => {
  vi.clearAllMocks(); saved = initial;
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div'); document.body.append(host);
  root = createRoot(host);
  await act(async () => root.render(<ReminderPreferenceForm clientId="example" version={3} />));
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });
const submit = async () => act(async () => { host.querySelector('form')!.dispatchEvent(
  new Event('submit', {bubbles: true, cancelable: true})); });
async function setValue(name: string, value: string) {
  await act(async () => {
    const field = host.querySelector<HTMLInputElement>(`input[name="${name}"]`)!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("records expressed enrollment separately from eligibility and handles stale saves", async () => {
  expect(host.textContent).toContain("Verify the current phone");
  expect(host.textContent).toContain("Record SMS consent");
  expect(host.textContent).toContain("STOP");
  expect(host.textContent).toContain("America/Los_Angeles");
  const checkbox = host.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  await act(async () => checkbox.click());
  request.mockResolvedValueOnce({ ...initial, enabled: true, version: 4 });
  await submit();
  expect(request).toHaveBeenCalledWith('/clients/example/reminder-preference', {
    method: 'PUT', body: { enabled: true, expected_version: 3 } });
  expect(checkbox.checked).toBe(true);
  expect(host.textContent).toContain("Reminder preference saved");
  await act(async () => checkbox.click());
  request.mockRejectedValueOnce(new ApiError('changed', 409, 'RECORD_CONFLICT', null));
  await submit();
  expect(host.textContent).toContain("This profile changed");
  expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
  await submit();
  expect(request).toHaveBeenCalledTimes(2);
  saved = { ...initial, enabled: true, version: 5, days_before: 4 };
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent === "Reload saved preference")!.click());
  expect(checkbox.checked).toBe(true);
  expect(host.querySelector<HTMLInputElement>('input[name="days_before"]')!.value).toBe("4");
  expect(host.textContent).not.toContain("This profile changed");
});

it("saves timing while off without changing enrollment, then reloads saved values", async () => {
  await setValue("days_before", "7");
  await setValue("local_time", "18:00");
  saved = { ...initial, version: 4, days_before: 7, local_time: "18:00:00" };
  request.mockResolvedValueOnce(saved);
  await submit();
  expect(request).toHaveBeenCalledWith('/clients/example/reminder-preference', {
    method: 'PUT', body: { expected_version: 3, days_before: 7, local_time: "18:00" } });
  expect(host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false);
  expect(host.textContent).toContain("Saved: Reminders off · 7 calendar days before at 18:00");
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent === "Reload saved preference")!.click());
  expect(host.querySelector<HTMLInputElement>('input[name="local_time"]')!.value).toBe("18:00");
});

it("turns enrollment off without replacing saved timing", async () => {
  saved = { ...initial, enabled: true, version: 4 };
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent === "Reload saved preference")!.click());
  await act(async () => host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  request.mockResolvedValueOnce({ ...saved, enabled: false, version: 5 });
  await submit();
  expect(request).toHaveBeenCalledWith('/clients/example/reminder-preference', {
    method: 'PUT', body: { expected_version: 4, enabled: false } });
  expect(host.querySelector<HTMLInputElement>('input[name="days_before"]')!.value).toBe("2");
  expect(host.querySelector<HTMLInputElement>('input[name="local_time"]')!.value).toBe("09:15");
});

it("rejects fractional days and out-of-range minutes before saving", async () => {
  for (const value of ["0", "8", "1.5"]) {
    await setValue("days_before", value);
    await submit();
    expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
  }
  await setValue("days_before", "1");
  for (const value of ["07:59", "18:01", "09:00:30"]) {
    await setValue("local_time", value);
    await submit();
    expect(host.textContent).toContain("Choose an integer");
  }
  expect(request).not.toHaveBeenCalled();
  await setValue("local_time", "08:00");
  expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(false);
});

it("distinguishes disabled, blocked, past-time and expected reminders without promising delivery", async () => {
  saved = { ...initial, upcoming_reminders: (["disabled", "blocked", "past_time", "scheduled", "attempted"] as const)
    .map((status) => ({ ...initial.upcoming_reminders[0]!, appointment_id: status, status })) };
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent === "Reload saved preference")!.click());
  expect(host.textContent).toContain("Reminders are off");
  expect(host.textContent).toContain("Delivery is currently blocked");
  expect(host.textContent).toContain("no catch-up reminder");
  expect(host.textContent).toContain("Reminder expected");
  expect(host.textContent).toContain("Already attempted; no repeat reminder");
  expect(host.textContent).toContain("Delivery is not guaranteed");
});

it("keeps enrollment controls available before business timezone setup", async () => {
  saved = { ...initial, timezone: null, blockers: ["business_unconfigured"], upcoming_reminders: [] };
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent === "Reload saved preference")!.click());
  expect(host.textContent).toContain("Business timezone (not configured)");
  expect(host.textContent).toContain("Configure business availability");
  expect(host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.disabled).toBe(false);
});
