import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { Workspace } from "@/components/Workspace";
import { OwnerApi } from "@/lib/api";
import { authorizeUrl, clearPendingSignIn, completeSignIn, logoutUrl, markSessionRejected, takeSessionRejected, tokenExpiry } from "@/lib/auth";
import { ConfigError, parseConfig, rawConfigFromEnv, type OwnerConfig } from "@/lib/config";
import { OwnerProvider, errorMessage } from "@/owner/OwnerContext";

function loadConfig(): OwnerConfig | ConfigError {
  try {
    return parseConfig(rawConfigFromEnv());
  } catch (error) {
    return error instanceof ConfigError ? error : new ConfigError(errorMessage(error));
  }
}

const config = loadConfig();

interface Notice { id: number; message: string; error: boolean }

let noticeCount = 0;

class SessionNoticeGate {
  private rejected = false;

  isRejected() { return this.rejected; }
  reject() {
    if (this.rejected) return false;
    this.rejected = true;
    return true;
  }
  reset() { this.rejected = false; }
}

export default function OwnerPage() {
  if (config instanceof ConfigError) {
    return <Shell signedIn={false} onAuth={null}><p className="notice error">{config.message}</p></Shell>;
  }
  return <OwnerSession config={config} />;
}

export function OwnerSession({ config }: { config: OwnerConfig }) {
  // The access token lives only in React state: never in storage, cookies, or the URL.
  const [token, setToken] = useState<string | null>(null);
  // The signed-in email, for display only. The ID token it came from is not kept.
  const [email, setEmail] = useState<string | null>(null);
  const [session, setSession] = useState(0);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [completing, setCompleting] = useState(true);
  // A rejected session owns the notice until a new sign-in. Late request handlers can
  // run before their provider unmounts, so the guard must take effect synchronously.
  const sessionGuard = useMemo(() => new SessionNoticeGate(), []);

  // An empty message clears the notice. Each notice gets a new id so a repeated message is announced again.
  const notify = useCallback((message: string, error = false) => {
    if (sessionGuard.isRejected()) return;
    setNotice(message ? { id: ++noticeCount, message, error } : null);
  }, [sessionGuard]);

  const endSession = useCallback((message: string | null) => {
    setToken(null);
    setEmail(null);
    setSession((value) => value + 1);
    clearPendingSignIn();
    setNotice(message ? { id: ++noticeCount, message, error: true } : null);
  }, []);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.search) window.history.replaceState(null, "", url.pathname);
    const pendingNotice = takeSessionRejected();
    completeSignIn(config, url)
      .then((result) => { if (pendingNotice) notify(pendingNotice, true); return result; })
      .then((result) => { if (result) { setEmail(result.email); setToken(result.accessToken); } })
      .catch((error: unknown) => notify(errorMessage(error), true))
      .finally(() => setCompleting(false));
  }, [config, notify]);

  // End the session when the access token expires, even if an expired-token 401 arrives
  // without CORS headers and the browser reports it only as a network failure.
  useEffect(() => {
    const expiry = token ? tokenExpiry(token) : null;
    if (expiry === null) return;
    const timer = window.setTimeout(() => {
      if (!sessionGuard.isRejected()) endSession("Your session expired. Sign in again.");
    }, Math.max(0, expiry - Date.now() - 30_000));
    return () => window.clearTimeout(timer);
  }, [token, endSession, sessionGuard]);

  // A 401 means an expired token or a Cognito user who is not the owner. The hosted UI
  // cookie would silently sign that same user in again, so end it through hosted UI logout
  // and return here; the next Sign in then asks for credentials.
  const rejectSession = useCallback(() => {
    if (!sessionGuard.reject()) return;
    const message = "Your session ended. Sign in again.";
    endSession(message);
    const url = logoutUrl(config, window.location.origin);
    if (url) {
      markSessionRejected(message);
      window.location.assign(url);
    }
  }, [config, endSession, sessionGuard]);

  // Parallel reads can all return 401; only the first one per token acts. The guard lives
  // with the api object, so a new token gets a fresh guard.
  const api = useMemo(() => {
    if (!token) return null;
    let acted = false;
    return new OwnerApi(config, token, () => {
      if (acted) return;
      acted = true;
      rejectSession();
    });
  }, [config, token, rejectSession]);

  const signIn = useCallback(() => {
    authorizeUrl(config, window.location.origin)
      .then((url) => window.location.assign(url))
      .catch((error: unknown) => notify(errorMessage(error), true));
  }, [config, notify]);

  const signOut = useCallback(() => {
    endSession(null);
    const url = logoutUrl(config, window.location.origin);
    if (url) window.location.assign(url);
  }, [config, endSession]);

  const signInLocally = useCallback((value: string) => {
    sessionGuard.reset();
    setToken(value);
  }, [sessionGuard]);

  // Signed in, the notice renders right under the sticky header (inside Workspace); signed out there is no header.
  const noticeBar = <NoticeBar notice={notice} onDismiss={() => setNotice(null)} />;
  return (
    <Shell announcement={notice} signedIn={api !== null} onAuth={api ? signOut : config.authMode === "cognito" ? signIn : null}>
      {!api && noticeBar}
      {api ? (
        <OwnerProvider key={session} api={api} notify={notify}><Workspace onSignOut={signOut} account={{ email, local: config.authMode === "local" }} notice={noticeBar} /></OwnerProvider>
      ) : completing ? (
        <p className="muted">Checking sign-in…</p>
      ) : config.authMode === "local" ? (
        <LocalSignIn onToken={signInLocally} />
      ) : (
        <section className="welcome card">
          <h2>Your schedule, in one place.</h2>
          <p>Sign in to review requests, manage appointments and unavailable time, and update client details.</p>
          <button className="primary" type="button" onClick={signIn}>Sign in</button>
        </section>
      )}
    </Shell>
  );
}

/**
 * Visible bar for save results and rejections. Errors stay pinned to the top of the viewport and
 * can be dismissed; successes scroll with the page. It sits directly below the
 * sticky header; the document's scroll padding (see globals.css) covers header plus notice so
 * keyboard focus is never scrolled underneath either.
 */
function NoticeBar({ notice, onDismiss }: { notice: Notice | null; onDismiss: () => void }) {
  const bar = useRef<HTMLDivElement>(null);
  const dismiss = useRef<HTMLButtonElement>(null);
  const before = useRef<HTMLElement | null>(null);
  const pinned = notice?.error === true;

  useLayoutEffect(() => {
    const root = document.documentElement;
    const element = bar.current;
    if (!pinned || !element) { root.style.removeProperty("--notice-height"); return; }
    const apply = () => root.style.setProperty("--notice-height", `${element.offsetHeight}px`);
    apply();
    window.addEventListener("resize", apply);
    return () => { window.removeEventListener("resize", apply); root.style.removeProperty("--notice-height"); };
  }, [pinned, notice?.id]);

  return (
    <div ref={bar} className={`notice${notice ? "" : " idle"}${notice?.error ? " error" : ""}${pinned ? " pinned" : ""}`}>
      {/* Presentational: the single live region is in Shell, so nothing is announced twice. */}
      <div className="notice-text">{notice?.message}</div>
      {pinned && (
        <button ref={dismiss} type="button" className="notice-dismiss" aria-label="Dismiss message"
          onFocus={(event) => {
            const from = event.relatedTarget;
            before.current = from instanceof HTMLElement ? from : null;
          }}
          onClick={(event) => {
            // Chrome focuses buttons on pointer press too; only keyboard activation
            // (detail === 0) should move focus when the button disappears.
            const keyboard = event.detail === 0 && document.activeElement === dismiss.current;
            const target = before.current;
            onDismiss();
            if (!keyboard) return;
            queueMicrotask(() => {
              const fallback = document.querySelector<HTMLElement>(".app-header-menu button");
              (target?.isConnected ? target : fallback)?.focus({ preventScroll: true });
            });
          }}>
          Dismiss
        </button>
      )}
    </div>
  );
}

/** Local synthetic development only: paste the LOCAL_OWNER_TOKEN; it is kept in memory. */
function LocalSignIn({ onToken }: { onToken: (token: string) => void }) {
  return (
    <section className="welcome card">
      <h2>Local development sign-in</h2>
      <p className="hint">Synthetic local API only. Paste the <code>LOCAL_OWNER_TOKEN</code> used to start it.</p>
      <form className="form-stack" onSubmit={(event) => {
        event.preventDefault();
        const value = new FormData(event.currentTarget).get("token");
        if (typeof value === "string" && value.trim()) onToken(value.trim());
      }}>
        <label>Local owner token <input name="token" type="password" autoComplete="off" required /></label>
        <button className="primary" type="submit">Sign in locally</button>
      </form>
    </section>
  );
}

function Shell({ signedIn, onAuth, announcement = null, children }: {
  signedIn: boolean; onAuth: (() => void) | null; announcement?: Notice | null; children: React.ReactNode;
}) {
  return (
    <>
      {/* The only live region on the page. It stays mounted across sign-in and sign-out so a message set
          while the visible bar changes place (for example "session ended") is still announced. The keyed
          child makes an identical repeated message announce again. */}
      <div role="status" aria-live="polite" className="visually-hidden">
        {announcement && <span key={announcement.id}>{announcement.message}</span>}
      </div>
      {/* Signed in, the workspace renders the calendar header with the menu and the account button (sign out). */}
      {!signedIn && (
        <header className="topbar">
          <div><span className="eyebrow">OWNER WORKSPACE</span><h1>Scheduling</h1></div>
          <div className="top-actions">
            <span>Signed out</span>
            {onAuth && <button type="button" onClick={onAuth}>Sign in</button>}
          </div>
        </header>
      )}
      <main>{children}</main>
      <footer>Scheduling pilot · Changes appear only after the server confirms them.</footer>
    </>
  );
}
