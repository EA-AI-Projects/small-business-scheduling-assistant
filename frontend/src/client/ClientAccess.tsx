import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/router";

import { AccountMenu } from "@/components/shell/AccountMenu";
import { AppHeader } from "@/components/shell/AppHeader";
import { authorizeUrl, clearPendingSignIn, completeSignIn, logoutUrl, tokenExpiry, type SignInResult } from "@/lib/auth";
import { ConfigError, parseConfig, rawConfigFromEnv, type OwnerConfig } from "@/lib/config";

/** Client menu sections. Add an entry here (and a page under `pages/client/`) to extend the menu. */
export const CLIENT_SECTIONS = [
  { id: "calendar", label: "Calendar", href: "/client/", pathname: "/client" },
  { id: "appointments", label: "Appointments", href: "/client/appointments/", pathname: "/client/appointments" },
] as const;

/** What a signed-in client page needs: the access token, the business zone and the end-of-session hook. */
export interface ClientSessionValue { config: OwnerConfig; token: string; zone: string | null; onSessionEnded: () => void }
const ClientSessionContext = createContext<ClientSessionValue | null>(null);
export function useClientSession(): ClientSessionValue {
  const value = useContext(ClientSessionContext);
  if (!value) throw new Error("useClientSession needs a signed-in client shell.");
  return value;
}

export const clientConfig = (() => {
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

/**
 * Layout for every client page. Next keeps it mounted while the client moves between pages, so the
 * in-memory sign-in survives menu navigation (it is never stored).
 */
export function clientLayout(page: ReactNode): ReactNode {
  return clientConfig instanceof ConfigError ? <ClientFrame><p role="alert">{clientConfig.message}</p></ClientFrame>
    : <ClientAccess config={clientConfig}>{page}</ClientAccess>;
}

export function ClientAccess({ config, children }: { config: OwnerConfig; children: ReactNode }) {
  return config.authMode === "local" ? <LocalClientAccess config={config}>{children}</LocalClientAccess>
    : <CognitoClientAccess config={config}>{children}</CognitoClientAccess>;
}

/** Signed-in frame: the shared header with the client menu and the account menu, then the page. */
function ClientShell({ value, email, local, onSignOut, notice, children }: {
  value: ClientSessionValue; email: string | null; local: boolean; onSignOut: () => void;
  notice?: ReactNode; children: ReactNode;
}) {
  const router = useRouter();
  const section = CLIENT_SECTIONS.find((item) => item.pathname === router.pathname.replace(/\/$/, "")) ?? CLIENT_SECTIONS[0];
  return <ClientSessionContext.Provider value={value}>
    <AppHeader appName="Smart Scheduling Assistant" current={section.id}
      items={CLIENT_SECTIONS.map(({ id, label }) => ({ id, label }))}
      onSelect={(id) => { const next = CLIENT_SECTIONS.find((item) => item.id === id); if (next) void router.push(next.href); }}
      trailing={<AccountMenu email={email} local={local} localLabel="Local test client (synthetic)" onSignOut={onSignOut} />}>
      <span className="range-title">{section.label}</span>
    </AppHeader>
    {notice}
    <main>{children}</main>
    <footer>Smart Scheduling Assistant pilot</footer>
  </ClientSessionContext.Provider>;
}

/** Local synthetic development only: paste a client token printed by the local backend. */
function LocalClientAccess({ config, children }: { config: OwnerConfig; children: ReactNode }) {
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

  const signedIn = session && token ? { config, token, zone: session.timezone ?? null,
    onSessionEnded: () => end("Your session ended. Sign in again.") } : null;
  if (signedIn) return <ClientShell value={signedIn} email={null} local onSignOut={() => end(null)}
    notice={message ? <div className="notice error" role="alert"><p>{message}</p></div> : undefined}>{children}</ClientShell>;

  return <ClientFrame>
    {message && <div className="notice error" role="alert"><p>{message}</p></div>}
    <section className="welcome card">
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
    </section>
  </ClientFrame>;
}

function CognitoClientAccess({ config, children }: { config: OwnerConfig; children: ReactNode }) {
  const [session, setSession] = useState<ClientSession | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [message, setMessage] = useState<string | null>(null);
  const callback = useRef<URL | null>(null);
  const completion = useRef<Promise<SignInResult | null> | null>(null);
  const router = useRouter();
  const routerRef = useRef(router);
  useEffect(() => { routerRef.current = router; }, [router]);

  const end = useCallback((notice: string | null, hostedLogout: boolean) => {
    setSession(null);
    setAccessToken(null);
    setEmail(null);
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
    // Next owns the history entry: its saved url/as must lose the used code and state too.
    if (url.search) void routerRef.current.replace(url.pathname, undefined, { shallow: true });
    completion.current ??= completeSignIn(config, url);
    completion.current.then(async (result) => {
      if (!result || !current) return;
      // The ID token is used only in this call stack to activate a pending invitation.
      const confirmed = await confirmClient(config, result.accessToken, result.idToken);
      if (current) { setAccessToken(result.accessToken); setEmail(result.email ?? null); setSession(confirmed); }
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

  const notice = message ? <div className="notice error" role="alert"><p>{message}</p>
    <button type="button" onClick={() => end(null, true)}>Sign out and switch account</button></div> : undefined;
  if (!busy && session && accessToken) {
    const value = { config, token: accessToken, zone: session.timezone ?? null,
      onSessionEnded: () => end("Your session ended. Sign in again.", true) };
    return <ClientShell value={value} email={email} local={false} onSignOut={() => end(null, true)} notice={notice}>
      {children}</ClientShell>;
  }

  return <ClientFrame>
    {notice}
    {busy ? <p>Checking sign-in…</p> : <section className="welcome card">
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
