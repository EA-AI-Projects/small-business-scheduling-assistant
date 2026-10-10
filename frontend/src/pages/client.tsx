import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import { ClientHome } from "@/components/client/ClientHome";
import { authorizeUrl, clearPendingSignIn, completeSignIn, logoutUrl, tokenExpiry, type SignInResult } from "@/lib/auth";
import { ConfigError, parseConfig, rawConfigFromEnv, type OwnerConfig } from "@/lib/config";

const config = (() => {
  try { return parseConfig(rawConfigFromEnv()); }
  catch { return new ConfigError("Account sign-in is not configured."); }
})();

type ClientSession = { role: "client"; business_id: string; client_id: string; timezone: string | null };

async function accountRequest(config: OwnerConfig, path: string, credential: string, method = "GET"): Promise<Response> {
  try {
    return await fetch(`${config.apiBaseUrl}${path}`, {
      method, headers: { Authorization: `Bearer ${credential}` }, credentials: "omit",
      cache: "no-store", referrerPolicy: "no-referrer",
    });
  } catch { throw new Error("Could not reach the account service. Please try again."); }
}

async function confirmClient(config: OwnerConfig, accessToken: string, idToken: string | null): Promise<ClientSession> {
  let response = await accountRequest(config, "/v1/client/session", accessToken);
  if (!response.ok && idToken && (response.status === 401 || response.status === 403)) {
    const activation = await accountRequest(config, "/v1/account/invitations/activate", idToken, "POST");
    if (activation.ok) response = await accountRequest(config, "/v1/client/session", accessToken);
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error("This account cannot access the client area. If your invitation expired or your access changed, contact the business owner.");
    }
    throw new Error("Could not confirm client access. Please try again.");
  }
  const data: unknown = await response.json().catch(() => null);
  if (typeof data !== "object" || data === null || (data as ClientSession).role !== "client") {
    throw new Error("Could not confirm client access. Please try again.");
  }
  return data as ClientSession;
}

export default function ClientPage() {
  return config instanceof ConfigError ? <ClientFrame><p role="alert">{config.message}</p></ClientFrame>
    : <ClientAccess config={config} />;
}

export function ClientAccess({ config }: { config: OwnerConfig }) {
  return config.authMode === "local" ? <LocalClientAccess config={config} /> : <CognitoClientAccess config={config} />;
}

/** Local synthetic development only: paste a client token printed by the local backend. */
function LocalClientAccess({ config }: { config: OwnerConfig }) {
  const [token, setToken] = useState<string | null>(null);
  const [session, setSession] = useState<ClientSession | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const end = useCallback((notice: string | null) => { setToken(null); setSession(null); setMessage(notice); }, []);

  const signIn = useCallback((value: string) => {
    setBusy(true);
    setMessage(null);
    confirmClient(config, value, null)
      .then((confirmed) => { setToken(value); setSession(confirmed); })
      .catch((error: unknown) => setMessage(error instanceof Error ? error.message : "Sign-in failed."))
      .finally(() => setBusy(false));
  }, [config]);

  return <ClientFrame>
    {message && <div className="notice error" role="alert"><p>{message}</p></div>}
    {session && token ? <>
      <section className="card"><h2>Welcome, local test client</h2>
        <p>Synthetic local mode. Choosing a time asks the owner for approval. Nothing is booked until the owner approves it.</p>
        <button type="button" className="primary" onClick={() => end(null)}>Sign out</button></section>
      <ClientHome config={config} token={token} zone={session.timezone ?? null}
        onSessionEnded={() => end("Your session ended. Sign in again.")} />
    </> : <section className="welcome card">
      <h2>Local client sign-in</h2>
      <p className="hint">Synthetic local API only. Paste one of the client tokens printed when the local backend started.</p>
      <form className="form-stack" onSubmit={(event) => {
        event.preventDefault();
        const value = String(new FormData(event.currentTarget).get("token") ?? "").trim();
        event.currentTarget.reset();
        if (value) signIn(value);
      }}>
        <label>Local client token <input name="token" type="password" autoComplete="off" required /></label>
        <button className="primary" type="submit" disabled={busy}>{busy ? "Checking…" : "Sign in locally"}</button>
      </form>
      <p>Business owner? <Link href="/">Use owner sign-in</Link>.</p>
    </section>}
  </ClientFrame>;
}

function CognitoClientAccess({ config }: { config: OwnerConfig }) {
  const [session, setSession] = useState<ClientSession | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [message, setMessage] = useState<string | null>(null);
  const callback = useRef<URL | null>(null);
  const completion = useRef<Promise<SignInResult | null> | null>(null);

  const end = useCallback((notice: string | null, hostedLogout: boolean) => {
    setSession(null);
    setAccessToken(null);
    clearPendingSignIn();
    setMessage(notice);
    if (hostedLogout) {
      const url = logoutUrl(config, window.location.origin, "/client/");
      if (url) window.location.assign(url);
    }
  }, [config]);

  useEffect(() => {
    let current = true;
    callback.current ??= new URL(window.location.href);
    const url = callback.current;
    if (url.search) window.history.replaceState(null, "", url.pathname);
    completion.current ??= completeSignIn(config, url);
    completion.current.then(async (result) => {
      if (!result || !current) return;
      // The ID token is used only in this call stack to activate a pending invitation.
      const confirmed = await confirmClient(config, result.accessToken, result.idToken);
      if (current) { setAccessToken(result.accessToken); setSession(confirmed); }
    }).catch((error: unknown) => { if (current) setMessage(error instanceof Error ? error.message : "Sign-in failed."); })
      .finally(() => { if (current) setBusy(false); });
    return () => { current = false; };
  }, [config]);

  useEffect(() => {
    if (!accessToken) return;
    const expiry = tokenExpiry(accessToken);
    if (expiry === null) return;
    const timer = window.setTimeout(() => end("Your session expired. Sign in again.", true),
      Math.max(0, expiry - Date.now() - 30_000));
    return () => window.clearTimeout(timer);
  }, [accessToken, end]);

  const redirect = useCallback((recovery: boolean) => {
    setMessage(null);
    authorizeUrl(config, window.location.origin, sessionStorage, "/client/", recovery)
      .then((url) => window.location.assign(url))
      .catch(() => setMessage("Could not start sign-in. Please try again."));
  }, [config]);

  return <ClientFrame>
    {message && <div className="notice error" role="alert"><p>{message}</p>
      <button type="button" onClick={() => end(null, true)}>Sign out and switch account</button></div>}
    {busy ? <p>Checking sign-in…</p> : session && accessToken ? <>
      <section className="card"><h2>Welcome to your client account</h2>
        <p>Choosing a time asks the owner for approval. Nothing is booked until the owner approves it.</p>
        <button type="button" className="primary" onClick={() => end(null, true)}>Sign out</button></section>
      <ClientHome config={config} token={accessToken} zone={session.timezone ?? null}
        onSessionEnded={() => end("Your session ended. Sign in again.", true)} />
    </> : <section className="welcome card">
      <h2>Client sign-in</h2>
      <p>Use the email invitation from the business to set your password and sign in. Invitations are valid for 24 hours after they are sent.</p>
      <button type="button" className="primary" onClick={() => redirect(false)}>Sign in or accept invitation</button>
      <p><button type="button" className="text-button" onClick={() => redirect(true)}>Forgot your password?</button></p>
      <p>Business owner? <Link href="/">Use owner sign-in</Link>.</p>
    </section>}
  </ClientFrame>;
}

function ClientFrame({ children }: { children: React.ReactNode }) {
  return <><header className="topbar"><div className="signed-out-brand"><span className="brand-mark" aria-hidden="true" />
    <div><span className="eyebrow">CLIENT ACCOUNT</span><h1>Smart Scheduling Assistant</h1></div></div></header>
    <main>{children}</main><footer>Smart Scheduling Assistant pilot</footer></>;
}
