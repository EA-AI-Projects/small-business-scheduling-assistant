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
    const reads = Array.from({ length: 6 }, () => pending<Response>());
    let started = 0;
    vi.stubGlobal("fetch", vi.fn(() => reads[started++]!.promise));
    await act(async () => root.render(<OwnerSession config={config} />));

    const form = host.querySelector("form")!;
    (form.querySelector("input") as HTMLInputElement).value = "synthetic-token";
    await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(started).toBe(1);
    await act(async () => reads[0]!.resolve(new Response("{}", { status: 200 })));
    expect(started).toBe(6);

    await act(async () => reads[1]!.resolve(new Response('{"error":{"message":"Unauthorized"}}', { status: 401 })));
    const message = "Your session ended or this account cannot access the owner workspace. Sign in with an approved owner account.";
    expect(host.querySelector(".notice-text")?.textContent).toBe(message);
    expect(host.querySelector('[role="status"]')?.textContent).toBe(message);

    await act(async () => {
      reads[2]!.resolve(new Response('{"error":{"message":"Later failure"}}', { status: 500 }));
      reads[3]!.resolve(new Response('{"error":{"message":"Another failure"}}', { status: 503 }));
      reads[4]!.resolve(new Response("{}", { status: 200 }));
      reads[5]!.resolve(new Response("{}", { status: 200 }));
    });
    expect(host.querySelector(".notice-text")?.textContent).toBe(message);
    expect(host.querySelector('[role="status"]')?.textContent).toBe(message);
  });

  it("ignores a late 401 from the previous token after a new local sign-in", async () => {
    const reads = Array.from({ length: 12 }, () => pending<Response>());
    let started = 0;
    const fetcher = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      expect(input).toBeTruthy();
      expect(init?.credentials).toBe("omit");
      return reads[started++]!.promise;
    });
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<OwnerSession config={config} />));

    async function signIn(token: string) {
      const form = host.querySelector("form")!;
      (form.querySelector("input") as HTMLInputElement).value = token;
      await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    }

    await signIn("synthetic-old-token");
    expect(started).toBe(1);
    await act(async () => reads[0]!.resolve(new Response("{}", { status: 200 })));
    expect(started).toBe(6);
    await act(async () => reads[1]!.resolve(new Response("{}", { status: 401 })));
    expect(host.querySelector("form")).not.toBeNull();

    await signIn("synthetic-new-token");
    expect(started).toBe(7);
    await act(async () => reads[6]!.resolve(new Response("{}", { status: 200 })));
    expect(started).toBe(12);
    expect(host.textContent).toContain("Workspace");
    expect(fetcher.mock.calls[6]?.[1]?.headers).toEqual({ Authorization: "Bearer synthetic-new-token" });

    await act(async () => reads[2]!.resolve(new Response("{}", { status: 401 })));
    expect(host.textContent).toContain("Workspace");
    expect(host.querySelector("form")).toBeNull();
  });
});
