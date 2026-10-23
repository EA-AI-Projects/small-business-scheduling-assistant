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
  let saved = { enabled: false, version: 1, blockers: ["phone_unverified", "consent_missing", "sms_opted_out"] };
  fetcher.mockImplementation(async (url: string, init: RequestInit) => {
    expect(url).toBe("https://api.example.test/v1/client/reminder-preference");
    expect(init.headers).toMatchObject({ Authorization: "Bearer access" });
    expect(init.credentials).toBe("omit");
    if (init.method === "PUT") {
      const body = JSON.parse(init.body as string);
      expect(Object.keys(body).sort()).toEqual(["enabled", "expected_version"]);
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
  fetcher.mockResolvedValueOnce(response({ enabled: false, version: 1, blockers: [] }));
  await render(); await act(async () => host.querySelector<HTMLInputElement>("input")!.click());
  fetcher.mockResolvedValueOnce(response({}, status)); await submit();
  expect(host.querySelector('[role="alert"]')).not.toBeNull();
  expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
  fetcher.mockResolvedValueOnce(response({ enabled: true, version: 3, blockers: [] }));
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
