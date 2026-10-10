import { describe, expect, it } from "vitest";

import { completeSignIn, emailFromIdToken } from "./auth";

// Boundary: reading the display-only email from the ID token returned by the token exchange.
// Not covered: the hosted UI redirect, and a real Cognito token (needs a deployed stack).
function segment(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}
const token = (claims: unknown) => `${segment({ alg: "none" })}.${segment(claims)}.signature`;

describe("emailFromIdToken", () => {
  it("reads the email claim from a valid payload", () => {
    expect(emailFromIdToken(token({ sub: "synthetic-1", email: "owner@example.test" }))).toBe("owner@example.test");
  });

  it("returns null when the claim is missing, empty, or not a string", () => {
    expect(emailFromIdToken(token({ sub: "synthetic-1" }))).toBeNull();
    expect(emailFromIdToken(token({ email: "  " }))).toBeNull();
    expect(emailFromIdToken(token({ email: 42 }))).toBeNull();
    expect(emailFromIdToken(token(["owner@example.test"]))).toBeNull();
  });

  it("returns null for malformed tokens instead of throwing", () => {
    expect(emailFromIdToken("")).toBeNull();
    expect(emailFromIdToken("not-a-jwt")).toBeNull();
    expect(emailFromIdToken("a.@@@.c")).toBeNull();
    expect(emailFromIdToken("a.bm90IGpzb24.c")).toBeNull();
  });

  it("decodes the base64url - and _ characters and UTF-8 text", () => {
    // Guards the -/_ to +// mapping and strict UTF-8 decoding. (atob accepts unpadded input, so padding is not what this tests.)
    for (const email of ["a@x.test", "ab@x.test", "abc@x.test", "ü?>~@x.test"]) {
      const payload = segment({ email });
      expect(payload).not.toContain("=");
      expect(emailFromIdToken(`h.${payload}.s`)).toBe(email);
    }
  });
});

describe("completeSignIn", () => {
  function setup(body: unknown) {
    const store = new Map([["owner-pkce-verifier", "v"], ["owner-oauth-state", "s"]]);
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k) } as unknown as Storage;
    const fetcher = (async () => new Response(JSON.stringify(body), { status: 200 })) as typeof fetch;
    const config = { authMode: "cognito", cognitoDomain: "https://auth.example.test", clientId: "client" } as never;
    return completeSignIn(config, new URL("https://app.example.test/?code=c&state=s"), storage, fetcher);
  }

  it("returns the access token and ephemeral ID token for activation", async () => {
    const idToken = token({ email: "owner@example.test" });
    const result = await setup({ access_token: "access", id_token: idToken });
    expect(result).toEqual({ accessToken: "access", email: "owner@example.test", idToken });
  });

  it("returns a null email when the response has no ID token", async () => {
    expect(await setup({ access_token: "access" })).toEqual({ accessToken: "access", email: null, idToken: null });
  });
});
