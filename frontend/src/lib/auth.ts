/**
 * Cognito hosted UI authorization-code flow with PKCE.
 *
 * Only the one-time PKCE verifier and OAuth state, and the fixed "session ended" notice text
 * left by a 401 (which ends the hosted UI session like Sign out), are kept in sessionStorage,
 * and only across a redirect. The access token is returned to the caller and must stay in memory.
 */
import type { OwnerConfig } from "./config";

const VERIFIER_KEY = "owner-pkce-verifier";
const STATE_KEY = "owner-oauth-state";
const ENDED_KEY = "owner-session-ended";

export class SignInError extends Error {}

function b64url(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function randomString(length: number): string {
  return b64url(crypto.getRandomValues(new Uint8Array(length)));
}

export function redirectUri(origin: string): string {
  return `${origin}/`;
}

async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return b64url(new Uint8Array(digest));
}

function cognito(config: OwnerConfig): { domain: string; clientId: string } {
  if (config.authMode !== "cognito" || !config.cognitoDomain || !config.clientId) {
    throw new SignInError("Cognito sign-in is not configured");
  }
  return { domain: config.cognitoDomain, clientId: config.clientId };
}

export async function authorizeUrl(config: OwnerConfig, origin: string,
  storage: Storage = sessionStorage): Promise<string> {
  const { domain, clientId } = cognito(config);
  if (!globalThis.crypto?.subtle) throw new SignInError("A secure browser context is required to sign in");
  const verifier = randomString(32);
  const state = randomString(24);
  storage.setItem(VERIFIER_KEY, verifier);
  storage.setItem(STATE_KEY, state);
  const url = new URL("/oauth2/authorize", domain);
  url.search = new URLSearchParams({
    response_type: "code", client_id: clientId, redirect_uri: redirectUri(origin),
    scope: "openid", state, code_challenge_method: "S256", code_challenge: await challengeFor(verifier),
  }).toString();
  return url.toString();
}

export function clearPendingSignIn(storage: Storage = sessionStorage): void {
  storage.removeItem(VERIFIER_KEY);
  storage.removeItem(STATE_KEY);
}

/**
 * Finish a redirect back from Cognito. Returns null when the URL carries no OAuth response.
 * The caller must remove the query string from the address bar before rendering.
 */
export async function completeSignIn(config: OwnerConfig, location: URL,
  storage: Storage = sessionStorage, fetcher: typeof fetch = fetch): Promise<string | null> {
  const code = location.searchParams.get("code");
  const returnedState = location.searchParams.get("state");
  const oauthError = location.searchParams.get("error");
  if (!code && !oauthError) return null;
  const verifier = storage.getItem(VERIFIER_KEY);
  const expectedState = storage.getItem(STATE_KEY);
  clearPendingSignIn(storage);
  if (oauthError) throw new SignInError(`Sign-in failed: ${oauthError}`);
  if (!verifier || !expectedState || expectedState !== returnedState) {
    throw new SignInError("Sign-in response did not match this browser session");
  }
  const { domain, clientId } = cognito(config);
  const response = await fetcher(new URL("/oauth2/token", domain).toString(), {
    method: "POST", credentials: "omit",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", client_id: clientId, code: code ?? "",
      redirect_uri: redirectUri(location.origin), code_verifier: verifier,
    }),
  });
  const tokens: unknown = await response.json().catch(() => null);
  const accessToken = typeof tokens === "object" && tokens !== null
    ? (tokens as { access_token?: unknown }).access_token : undefined;
  if (!response.ok || typeof accessToken !== "string" || !accessToken) {
    throw new SignInError("Could not complete sign-in");
  }
  return accessToken;
}

/**
 * Expiry (ms since epoch) from a JWT access token's `exp` claim, or null if unreadable.
 * Read only to end the in-memory session on time; the API still verifies every token.
 */
export function tokenExpiry(token: string): number | null {
  const payload = token.split(".")[1];
  if (!payload) return null;
  try {
    const json = atob(payload.replaceAll("-", "+").replaceAll("_", "/"));
    const exp: unknown = (JSON.parse(json) as { exp?: unknown }).exp;
    return typeof exp === "number" && Number.isFinite(exp) ? exp * 1000 : null;
  } catch {
    return null;
  }
}

/** Hosted UI logout ends the Cognito session so the next sign-in asks for credentials. */
export function logoutUrl(config: OwnerConfig, origin: string): string | null {
  if (config.authMode !== "cognito" || !config.cognitoDomain || !config.clientId) return null;
  const url = new URL("/logout", config.cognitoDomain);
  url.search = new URLSearchParams({ client_id: config.clientId, logout_uri: redirectUri(origin) }).toString();
  return url.toString();
}

/**
 * Remember, across the hosted UI logout redirect, the notice message shown after the API
 * rejected the session (401). Only this message is stored, never the token.
 */
export function markSessionRejected(message: string, storage: Storage = sessionStorage): void {
  storage.setItem(ENDED_KEY, message);
}

/** Read and clear the message left by markSessionRejected, or null. */
export function takeSessionRejected(storage: Storage = sessionStorage): string | null {
  const message = storage.getItem(ENDED_KEY);
  storage.removeItem(ENDED_KEY);
  return message;
}
