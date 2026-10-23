// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ApiError } from "@/lib/api";
import { ReminderForm } from "./ReminderForm";

let saved = { version: 0, settings: { days_before: 1, local_time: "10:00:00" } };
const get = vi.fn(async () => saved);
const request = vi.fn(async (_path: string, options: {body: typeof saved}) => {
  saved = { ...options.body, version: options.body.version + 1 };
  return saved;
});
const api = { get, request };
vi.mock("@/owner/OwnerContext", () => ({ useOwner: () => ({ api,
  data: { zone: "America/Los_Angeles" } }) }));

it("loads defaults, saves valid boundaries, rejects invalid input and preserves drafts on conflicts", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement("div"); document.body.append(host);
  const root: Root = createRoot(host);
  await act(async () => root.render(<ReminderForm />));
  expect(host.textContent).toContain("America/Los_Angeles");
  const days = host.querySelector<HTMLInputElement>('input[type="number"]')!;
  const time = host.querySelector<HTMLInputElement>('input[type="time"]')!;
  const change = async (input: HTMLInputElement, value: string) => act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(days.value).toBe("1"); expect(time.value).toBe("10:00");
  await change(days, "7"); await change(time, "18:00");
  request.mockResolvedValueOnce({ version: 1, settings: { days_before: 7, local_time: "18:00:00" } });
  await act(async () => host.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(request).toHaveBeenCalledWith('/reminders', expect.objectContaining({ body: {
    expected_version: 0, settings: { days_before: 7, local_time: "18:00" } } }));
  expect(host.textContent).toContain("Reminder timing saved");
  await change(time, "18:01");
  expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
  await change(time, "08:00");
  request.mockRejectedValueOnce(new ApiError("changed", 409, "REVISION_CONFLICT", null));
  await act(async () => host.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(host.textContent).toContain("Reload saved settings");
  expect(time.value).toBe("08:00");
  await act(async () => root.unmount()); host.remove();
});
