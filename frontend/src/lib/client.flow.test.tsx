// @vitest-environment jsdom
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";

import { parseConfig } from "@/lib/config";
import { ClientAccess } from "@/pages/client";

const auth = vi.hoisted(() => ({
  completeSignIn: vi.fn(), authorizeUrl: vi.fn(), logoutUrl: vi.fn(),
}));
vi.mock("@/lib/auth", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/auth")>(), ...auth,
}));

const config = parseConfig({ authMode: "cognito", apiBaseUrl: "https://api.example.test",
  businessId: "pilot", cognitoDomain: "https://auth.example.test", clientId: "public" });

describe("client account entry", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.append(host); root = createRoot(host);
    auth.completeSignIn.mockReset(); auth.authorizeUrl.mockReset(); auth.logoutUrl.mockReset();
  });
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });

  it("activates an invited identity before showing the client landing", async () => {
    auth.completeSignIn.mockResolvedValue({ accessToken: "access", idToken: "id", email: "client@example.test" });
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response("{}", { status: 401 }))
      .mockResolvedValueOnce(new Response('{"activated":true}', { status: 200 }))
      .mockResolvedValueOnce(new Response('{"role":"client","business_id":"pilot","client_id":"synthetic"}', { status: 200 }))
      .mockImplementation(async (url: string) => new Response(
        url.includes("availability") ? '{"starts_at":[]}' : '{"bookings":[]}', { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<StrictMode><ClientAccess config={config} /></StrictMode>));
    expect(host.textContent).toContain("Welcome to your client account");
    expect(fetcher.mock.calls.map(([url]) => url).slice(0, 3)).toEqual([
      "https://api.example.test/v1/client/session",
      "https://api.example.test/v1/account/invitations/activate",
      "https://api.example.test/v1/client/session",
    ]);
    expect(fetcher.mock.calls[1]?.[1]?.headers).toEqual({ Authorization: "Bearer id" });
    expect(fetcher.mock.calls[2]?.[1]?.headers).toEqual({ Authorization: "Bearer access" });
  });

  it("shows generic denial for a wrong-role account and never mounts client content", async () => {
    auth.completeSignIn.mockResolvedValue({ accessToken: "owner-access", idToken: "owner-id", email: null });
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response("{}", { status: 403 }))
      .mockResolvedValueOnce(new Response("{}", { status: 403 })));
    await act(async () => root.render(<ClientAccess config={config} />));
    expect(host.textContent).toContain("cannot access the client area");
    expect(host.textContent).not.toContain("Welcome to your client account");
  });

  it("starts email recovery on the client callback path", async () => {
    auth.completeSignIn.mockResolvedValue(null);
    auth.authorizeUrl.mockReturnValue(new Promise(() => {}));
    await act(async () => root.render(<ClientAccess config={config} />));
    const recovery = Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.includes("Forgot"))!;
    await act(async () => recovery.click());
    expect(auth.authorizeUrl).toHaveBeenCalledWith(config, window.location.origin, sessionStorage, "/client/", true);
  });

  describe("local mode", () => {
    const local = parseConfig({ authMode: "local", apiBaseUrl: "http://127.0.0.1:8000", businessId: "pilot" });
    const submit = async (value: string) => {
      const input = host.querySelector<HTMLInputElement>('input[name="token"]');
      const form = host.querySelector("form");
      expect(input).not.toBeNull();
      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
        setter?.call(input, value);
        input?.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => { form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    };

    it("signs in with a pasted token, never touches Cognito or storage, and shows the client home", async () => {
      const fetcher = vi.fn().mockImplementation(async (url: string) => new Response(
        url.endsWith("/session") ? '{"role":"client","business_id":"pilot","client_id":"client-1","timezone":"America/Los_Angeles"}'
          : url.includes("availability") ? '{"starts_at":[]}' : '{"bookings":[]}', { status: 200 }));
      vi.stubGlobal("fetch", fetcher);
      await act(async () => root.render(<ClientAccess config={local} />));
      expect(host.textContent).toContain("Local client sign-in");
      await submit("pasted-local-client-token");
      expect(host.textContent).toContain("Welcome, local test client");
      expect(fetcher.mock.calls[0]?.[0]).toBe("http://127.0.0.1:8000/v1/client/session");
      expect(fetcher.mock.calls[0]?.[1]?.headers).toEqual({ Authorization: "Bearer pasted-local-client-token" });
      expect(fetcher.mock.calls.every(([url]) => !String(url).includes("/v1/account/"))).toBe(true);
      expect(auth.completeSignIn).not.toHaveBeenCalled();
      expect(auth.authorizeUrl).not.toHaveBeenCalled();
      expect(JSON.stringify({ ...sessionStorage })).not.toContain("pasted-local-client-token");
    });

    it("shows a denial and no client content for a rejected token", async () => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 401 })));
      await act(async () => root.render(<ClientAccess config={local} />));
      await submit("wrong-token");
      expect(host.textContent).toContain("cannot access the client area");
      expect(host.textContent).not.toContain("Welcome, local test client");
    });
  });

  it("keeps Cognito mode on the hosted sign-in, not the local form", async () => {
    auth.completeSignIn.mockResolvedValue(null);
    await act(async () => root.render(<ClientAccess config={config} />));
    expect(host.textContent).toContain("Sign in or accept invitation");
    expect(host.textContent).not.toContain("Local client sign-in");
    expect(host.querySelector('input[name="token"]')).toBeNull();
  });
});
