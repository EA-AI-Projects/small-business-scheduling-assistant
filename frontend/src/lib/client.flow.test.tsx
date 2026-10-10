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
      .mockResolvedValueOnce(new Response('{"role":"client","business_id":"pilot","client_id":"synthetic"}', { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<StrictMode><ClientAccess config={config} /></StrictMode>));
    expect(host.textContent).toContain("Welcome to your client account");
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
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
});
