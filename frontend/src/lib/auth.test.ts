import { authorizeUrl, completeSignIn, logoutUrl, tokenExpiry } from "./auth";
import { parseConfig } from "./config";

const config = parseConfig({
  authMode: "cognito",
  apiBaseUrl: "https://api.example.com",
  businessId: "pilot",
  cognitoDomain: "https://login.example.com",
  clientId: "public-client",
});
const ORIGIN = "https://owner.example.com";

function tokenResponse(body: unknown, ok = true): typeof fetch {
  return vi.fn(async () => new Response(JSON.stringify(body), { status: ok ? 200 : 400 }));
}

beforeEach(() => sessionStorage.clear());

describe("authorizeUrl", () => {
  it("builds a PKCE S256 request and stores only the verifier and state", async () => {
    const url = new URL(await authorizeUrl(config, ORIGIN));
    expect(url.origin + url.pathname).toBe("https://login.example.com/oauth2/authorize");
    expect(Object.fromEntries(url.searchParams)).toMatchObject({
      response_type: "code", client_id: "public-client", redirect_uri: `${ORIGIN}/`,
      scope: "openid", code_challenge_method: "S256",
    });
    expect(url.searchParams.get("state")).toBe(sessionStorage.getItem("owner-oauth-state"));
    expect(sessionStorage.length).toBe(2);
  });
});

describe("completeSignIn", () => {
  it("returns null when the URL has no OAuth response", async () => {
    expect(await completeSignIn(config, new URL(`${ORIGIN}/`))).toBeNull();
  });

  it("exchanges the code with the stored verifier and clears it", async () => {
    await authorizeUrl(config, ORIGIN);
    const state = sessionStorage.getItem("owner-oauth-state");
    const verifier = sessionStorage.getItem("owner-pkce-verifier");
    const fetcher = tokenResponse({ access_token: "access-1", id_token: "ignored" });
    const token = await completeSignIn(config, new URL(`${ORIGIN}/?code=abc&state=${state}`),
      sessionStorage, fetcher);
    expect(token).toBe("access-1");
    expect(sessionStorage.length).toBe(0);
    const [url, init] = vi.mocked(fetcher).mock.calls[0]!;
    expect(url).toBe("https://login.example.com/oauth2/token");
    const body = init?.body as URLSearchParams;
    expect(body.get("code_verifier")).toBe(verifier);
    expect(body.get("redirect_uri")).toBe(`${ORIGIN}/`);
    expect(init?.credentials).toBe("omit");
  });

  it("rejects a mismatched state without calling Cognito", async () => {
    await authorizeUrl(config, ORIGIN);
    const fetcher = tokenResponse({ access_token: "x" });
    await expect(completeSignIn(config, new URL(`${ORIGIN}/?code=abc&state=forged`),
      sessionStorage, fetcher)).rejects.toThrow("did not match");
    expect(fetcher).not.toHaveBeenCalled();
    expect(sessionStorage.length).toBe(0);
  });

  it("reports an OAuth error and a failed token exchange", async () => {
    await expect(completeSignIn(config, new URL(`${ORIGIN}/?error=access_denied`)))
      .rejects.toThrow("access_denied");
    await authorizeUrl(config, ORIGIN);
    const state = sessionStorage.getItem("owner-oauth-state");
    await expect(completeSignIn(config, new URL(`${ORIGIN}/?code=abc&state=${state}`),
      sessionStorage, tokenResponse({ error: "invalid_grant" }, false))).rejects.toThrow("Could not complete");
  });
});

describe("logoutUrl", () => {
  it("returns to the app origin through the hosted UI", () => {
    const url = new URL(logoutUrl(config, ORIGIN)!);
    expect(url.pathname).toBe("/logout");
    expect(url.searchParams.get("logout_uri")).toBe(`${ORIGIN}/`);
  });
});

describe("tokenExpiry", () => {
  const jwt = (payload: object) =>
    `h.${btoa(JSON.stringify(payload)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "")}.s`;

  it("reads the exp claim in milliseconds", () => {
    expect(tokenExpiry(jwt({ exp: 1_800_000_000, sub: "owner" }))).toBe(1_800_000_000_000);
  });

  it("returns null for opaque or malformed tokens", () => {
    expect(tokenExpiry("local-dev-token-0123456789")).toBeNull();
    expect(tokenExpiry("a.!!!.b")).toBeNull();
    expect(tokenExpiry(jwt({ exp: "soon" }))).toBeNull();
  });
});
