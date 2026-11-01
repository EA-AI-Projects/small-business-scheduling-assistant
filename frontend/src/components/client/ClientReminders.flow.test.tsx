// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { ClientReminders } from "./ClientReminders";
import type { OwnerConfig } from "@/lib/config";

const config = { apiBaseUrl: "https://api.example.test" } as OwnerConfig;
const ended = vi.fn();
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
let host: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
const fetcher = vi.fn();
const base = { days_before: 1, local_time: "10:00:00", timezone: "America/Los_Angeles", upcoming_reminders: [] };
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("fetch", fetcher); fetcher.mockReset(); ended.mockReset();
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });
const render = () => act(async () => root.render(<ClientReminders config={config} token="access" onSessionEnded={ended} />));
const submit = () => act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
const reload = () => act(async () => Array.from(host.querySelectorAll("button")).find((b) => b.textContent === "Reload saved preference")!.click());

it("loads, enrolls, reloads and unenrolls the shared preference while explaining SMS restrictions", async () => {
  let saved = { ...base, enabled: false, version: 1, blockers: ["phone_unverified", "consent_missing", "sms_opted_out"] };
  fetcher.mockImplementation(async (url: string, init: RequestInit) => {
    expect(url).toBe("https://api.example.test/v1/client/reminder-preference");
    expect(init.headers).toMatchObject({ Authorization: "Bearer access" });
    expect(init.credentials).toBe("omit");
    if (init.method === "PUT") {
      const body = JSON.parse(init.body as string);
      expect(Object.keys(body).sort()).toEqual(["enabled", "expected_version"]); // timing is untouched
      expect(body.expected_version).toBe(saved.version);
      saved = { ...saved, enabled: body.enabled, version: saved.version + 1 };
    }
    return response(saved);
  });
  await render();
  const checkbox = () => host.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  expect(checkbox().checked).toBe(false);
  expect(host.textContent).toContain("STOP");
  expect(host.textContent).toContain("Verify the current phone");
  await act(async () => checkbox().click()); await submit();
  expect(host.textContent).toContain("Reminder preference saved");
  await reload(); expect(checkbox().checked).toBe(true);
  await act(async () => checkbox().click()); await submit();
  expect(saved.enabled).toBe(false);
  expect(host.textContent).toContain("STOP");
});

it.each([409, 503])("requires reload after a %s save error and preserves the newer saved choice", async (status) => {
  fetcher.mockResolvedValueOnce(response({ ...base, enabled: false, version: 1, blockers: [] }));
  await render(); await act(async () => host.querySelector<HTMLInputElement>("input")!.click());
  fetcher.mockResolvedValueOnce(response({}, status)); await submit();
  expect(host.querySelector('[role="alert"]')).not.toBeNull();
  expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
  fetcher.mockResolvedValueOnce(response({ ...base, enabled: true, version: 3, blockers: [] }));
  await reload();
  expect(host.querySelector<HTMLInputElement>("input")!.checked).toBe(true);
  expect(host.querySelector('[role="alert"]')).toBeNull();
});

it("can reload after a read failure and ends unauthorized sessions", async () => {
  fetcher.mockRejectedValueOnce(new Error("offline")); await render();
  expect(host.querySelector('[role="alert"]')).not.toBeNull();
  fetcher.mockResolvedValueOnce(response({}, 401)); await reload();
  expect(ended).toHaveBeenCalledOnce();
});

const field = (name: string) => host.querySelector<HTMLInputElement>(`input[name="${name}"]`)!;
const type = (input: HTMLInputElement, value: string) => act(async () => {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
});
const saveButton = () => host.querySelector<HTMLButtonElement>('button[type="submit"]')!;

it("shows the timezone and saved timing, and sends only the changed timing so enrollment is kept", async () => {
  let saved = { ...base, enabled: true, version: 4, blockers: [] as string[] };
  const bodies: Record<string, unknown>[] = [];
  fetcher.mockImplementation(async (_url: string, init: RequestInit) => {
    if (init.method === "PUT") {
      const body = JSON.parse(init.body as string); bodies.push(body);
      saved = { ...saved, version: saved.version + 1, days_before: body.days_before ?? saved.days_before,
        local_time: body.local_time ? `${body.local_time}:00` : saved.local_time };
    }
    return response(saved);
  });
  await render();
  expect(host.textContent).toContain("America/Los_Angeles");
  expect(host.textContent).toContain("Saved: Enrolled · 1 calendar day before at 10:00");
  expect(saveButton().disabled).toBe(true);
  await type(field("days_before"), "3"); await type(field("local_time"), "08:30"); await submit();
  expect(bodies).toEqual([{ expected_version: 4, days_before: 3, local_time: "08:30" }]);
  expect(host.textContent).toContain("Saved: Enrolled · 3 calendar days before at 08:30");
  await type(field("local_time"), "18:00"); await submit();
  expect(bodies[1]).toEqual({ expected_version: 5, local_time: "18:00" });
  await reload(); expect(field("days_before").value).toBe("3"); expect(field("local_time").value).toBe("18:00");
});

it.each([["0", "10:00"], ["8", "10:00"], ["2", "07:59"], ["2", "18:01"]])(
  "blocks out-of-range timing %s %s before any save", async (days, time) => {
    fetcher.mockResolvedValueOnce(response({ ...base, enabled: false, version: 1, blockers: [] }));
    await render();
    await type(field("days_before"), days); await type(field("local_time"), time);
    expect(saveButton().disabled).toBe(true);
    expect(host.querySelector('[role="alert"]')).not.toBeNull();
    await submit();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

it("keeps the client's edits and explains a server validation refusal", async () => {
  fetcher.mockResolvedValueOnce(response({ ...base, enabled: false, version: 1, blockers: [] }));
  await render(); await type(field("days_before"), "5");
  fetcher.mockResolvedValueOnce(response({}, 422)); await submit();
  expect(host.querySelector('[role="alert"]')!.textContent).toContain("1–7");
  expect(field("days_before").value).toBe("5");
  expect(host.textContent).toContain("Saved: Reminders off · 1 calendar day");
});
