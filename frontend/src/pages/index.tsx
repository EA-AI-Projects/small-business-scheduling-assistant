import { useCallback, useEffect, useMemo, useState } from "react";

import { Workspace } from "@/components/Workspace";
import { OwnerApi } from "@/lib/api";
import { authorizeUrl, clearPendingSignIn, completeSignIn, logoutUrl } from "@/lib/auth";
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

interface Notice { message: string; error: boolean }

export default function OwnerPage() {
  if (config instanceof ConfigError) {
    return <Shell signedIn={false} onAuth={null}><p className="notice error">{config.message}</p></Shell>;
  }
  return <OwnerSession config={config} />;
}

function OwnerSession({ config }: { config: OwnerConfig }) {
  // The access token lives only in React state: never in storage, cookies, or the URL.
  const [token, setToken] = useState<string | null>(null);
  const [session, setSession] = useState(0);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [completing, setCompleting] = useState(true);

  const notify = useCallback((message: string, error = false) => setNotice({ message, error }), []);

  const endSession = useCallback((message: string | null) => {
    setToken(null);
    setSession((value) => value + 1);
    clearPendingSignIn();
    setNotice(message ? { message, error: true } : null);
  }, []);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.search) window.history.replaceState(null, "", url.pathname);
    completeSignIn(config, url)
      .then((accessToken) => { if (accessToken) setToken(accessToken); })
      .catch((error: unknown) => notify(errorMessage(error), true))
      .finally(() => setCompleting(false));
  }, [config, notify]);

  const api = useMemo(() => token
    ? new OwnerApi(config, token, () => endSession("Your session ended. Sign in again."))
    : null, [config, token, endSession]);

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

  return (
    <Shell signedIn={api !== null} onAuth={api ? signOut : config.authMode === "cognito" ? signIn : null}>
      <div className={`notice${notice?.error ? " error" : ""}`} role="status" aria-live="polite">
        {notice?.message}
      </div>
      {api ? (
        <OwnerProvider key={session} api={api} notify={notify}><Workspace /></OwnerProvider>
      ) : completing ? (
        <p className="muted">Checking sign-in…</p>
      ) : config.authMode === "local" ? (
        <LocalSignIn onToken={setToken} />
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

function Shell({ signedIn, onAuth, children }: {
  signedIn: boolean; onAuth: (() => void) | null; children: React.ReactNode;
}) {
  return (
    <>
      <header className="topbar">
        <div><span className="eyebrow">OWNER WORKSPACE</span><h1>Scheduling</h1></div>
        <div className="top-actions">
          <span>{signedIn ? "Owner signed in" : "Signed out"}</span>
          {onAuth && <button type="button" onClick={onAuth}>{signedIn ? "Sign out" : "Sign in"}</button>}
        </div>
      </header>
      <main>{children}</main>
      <footer>Scheduling pilot · Changes appear only after the server confirms them.</footer>
    </>
  );
}
