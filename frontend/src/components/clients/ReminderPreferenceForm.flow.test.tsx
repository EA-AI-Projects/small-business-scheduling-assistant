// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { ApiError } from "@/lib/api";
import { ReminderPreferenceForm } from "./ReminderPreferenceForm";

const get = vi.fn(async () => ({ enabled: false, version: 3,
  blockers: ['phone_unverified', 'consent_missing', 'sms_opted_out'] }));
const request = vi.fn();
const refresh = vi.fn();
const api = { get, request };
vi.mock("@/owner/OwnerContext", () => ({ useOwner: () => ({ api, refresh }) }));

it("records expressed enrollment separately from eligibility and handles stale saves", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement('div'); document.body.append(host);
  const root = createRoot(host);
  await act(async () => root.render(<ReminderPreferenceForm clientId="example" version={3} />));
  expect(host.textContent).toContain("Verify the current phone");
  expect(host.textContent).toContain("Record SMS consent");
  expect(host.textContent).toContain("STOP");
  const checkbox = host.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  await act(async () => checkbox.click());
  request.mockResolvedValueOnce({ enabled: true, version: 4,
    blockers: ['phone_unverified', 'consent_missing', 'sms_opted_out'] });
  const submit = async () => act(async () => host.querySelector('form')!.dispatchEvent(
    new Event('submit', {bubbles: true, cancelable: true})));
  await submit();
  expect(request).toHaveBeenCalledWith('/clients/example/reminder-preference', {
    method: 'PUT', body: { enabled: true, expected_version: 3 } });
  expect(checkbox.checked).toBe(true);
  expect(host.textContent).toContain("Reminder preference saved");
  await act(async () => checkbox.click());
  request.mockRejectedValueOnce(new ApiError('changed', 409, 'RECORD_CONFLICT', null));
  await submit();
  expect(host.textContent).toContain("Reload saved preference");
  await act(async () => root.unmount()); host.remove();
});
