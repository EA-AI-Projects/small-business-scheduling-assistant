// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { parseConfig } from "@/lib/config";

import { OwnerSession } from "@/pages/index";

vi.mock("@/components/Workspace", () => ({ Workspace: () => <div>Workspace</div> }));

const config = parseConfig({ authMode: "local", apiBaseUrl: "http://127.0.0.1:8000", businessId: "synthetic" });

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("session rejection notice", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  it("keeps the visible and announced session message after parallel reads fail", async () => {
    const reads = Array.from({ length: 4 }, () => pending<Response>());
    let started = 0;
    vi.stubGlobal("fetch", vi.fn(() => reads[started++]!.promise));
    await act(async () => root.render(<OwnerSession config={config} />));

    const form = host.querySelector("form")!;
    (form.querySelector("input") as HTMLInputElement).value = "synthetic-token";
    await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(started).toBe(4);

    await act(async () => reads[0]!.resolve(new Response('{"error":{"message":"Unauthorized"}}', { status: 401 })));
    const message = "Your session ended. Sign in again.";
    expect(host.querySelector(".notice-text")?.textContent).toBe(message);
    expect(host.querySelector('[role="status"]')?.textContent).toBe(message);

    await act(async () => {
      reads[1]!.resolve(new Response('{"error":{"message":"Later failure"}}', { status: 500 }));
      reads[2]!.resolve(new Response('{"error":{"message":"Another failure"}}', { status: 503 }));
      reads[3]!.resolve(new Response("{}", { status: 200 }));
    });
    expect(host.querySelector(".notice-text")?.textContent).toBe(message);
    expect(host.querySelector('[role="status"]')?.textContent).toBe(message);
  });
});
