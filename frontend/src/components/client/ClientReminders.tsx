import { useEffect, useRef, useState } from "react";
import type { OwnerConfig } from "@/lib/config";
import { reminderRestriction, type ReminderPreference } from "@/lib/reminders";

/** Authenticated client's shared preference; enrolling never changes SMS consent or STOP. */
export function ClientReminders({ config, token, onSessionEnded }: {
  config: OwnerConfig; token: string; onSessionEnded: () => void;
}) {
  const [saved, setSaved] = useState<ReminderPreference | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const sessionEnded = useRef(onSessionEnded);
  useEffect(() => { sessionEnded.current = onSessionEnded; }, [onSessionEnded]);
  useEffect(() => {
    let live = true;
    fetch(`${config.apiBaseUrl}/v1/client/reminder-preference`, {
      headers: { Authorization: `Bearer ${token}` }, credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
    }).then(async (response) => {
      if (!live) return;
      if (response.status === 401 || response.status === 403) { sessionEnded.current(); return; }
      if (!response.ok) throw new Error("read failed");
      const state = parsePreference(await response.json());
      if (live) { setSaved(state); setEnabled(state.enabled); setError(null); }
    }).catch(() => { if (live) setError("Your reminder preference could not load. Reload saved preference to try again."); });
    return () => { live = false; };
  }, [config.apiBaseUrl, token, attempt]);

  return <section className="card stack">
    <h2>Visit reminders</h2>
    <p>Choose whether to receive a text reminder before confirmed visits. The business sets the reminder timing.</p>
    <p>Enabling reminders does not verify your phone, grant SMS consent, or clear STOP. Contact the business if your phone or consent needs updating.</p>
    <form className="stack" onSubmit={async (event) => {
      event.preventDefault();
      if (!saved || busy || error) return;
      setBusy(true); setMessage(null);
      try {
        const response = await fetch(`${config.apiBaseUrl}/v1/client/reminder-preference`, {
          method: "PUT", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ enabled, expected_version: saved.version }),
          credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
        });
        if (response.status === 401 || response.status === 403) { sessionEnded.current(); return; }
        if (response.status === 409) {
          setError("Your profile changed. Reload saved preference before saving again."); return;
        }
        if (!response.ok) throw new Error("save failed");
        const state = parsePreference(await response.json());
        setSaved(state); setEnabled(state.enabled); setMessage("Reminder preference saved.");
      } catch { setError("Could not confirm your save. Reload saved preference before trying again."); }
      finally { setBusy(false); }
    }}>
      <label><input type="checkbox" checked={enabled} disabled={!saved || busy || Boolean(error)}
        onChange={(event) => { setEnabled(event.target.checked); setMessage(null); }} />Receive visit reminders</label>
      {saved?.blockers.length ? <div><p>Reminders cannot currently be sent:</p><ul>
        {saved.blockers.map((reason) => <li key={reason}>{reminderRestriction[reason] ?? "SMS delivery is restricted."}</li>)}
      </ul></div> : saved && <p>Your current profile and SMS consent allow reminders when enrolled.</p>}
      {error && <p role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
      {!saved && !error && <p role="status">Loading reminder preference…</p>}
      <button type="submit" disabled={!saved || busy || Boolean(error) || enabled === saved.enabled}>Save reminder preference</button>
      <button type="button" disabled={busy} onClick={() => { setSaved(null); setMessage(null); setError(null); setAttempt((value) => value + 1); }}>Reload saved preference</button>
    </form>
  </section>;
}

function parsePreference(value: unknown): ReminderPreference {
  const state = value as Partial<ReminderPreference> | null;
  if (!state || typeof state.enabled !== "boolean" || !Number.isInteger(state.version)
    || !Array.isArray(state.blockers) || !state.blockers.every((reason) => typeof reason === "string")) {
    throw new Error("Unexpected reminder preference response");
  }
  return state as ReminderPreference;
}
