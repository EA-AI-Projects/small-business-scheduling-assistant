import { useEffect, useRef, useState } from "react";
import type { components } from "@/api/schema";
import type { OwnerConfig } from "@/lib/config";
import { reminderRestriction, type ReminderPreference } from "@/lib/reminders";

type PreferenceBody = components["schemas"]["ClientReminderPreferenceBody"];
const TIME_PATTERN = /^(?:0[89]|1[0-7]):[0-5][0-9]$|^18:00$/;
const hhmm = (value: string) => value.slice(0, 5);

/** Authenticated client's shared preference; saving never changes SMS consent, phone verification or STOP. */
export function ClientReminders({ config, token, onSessionEnded }: {
  config: OwnerConfig; token: string; onSessionEnded: () => void;
}) {
  const [saved, setSaved] = useState<ReminderPreference | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [days, setDays] = useState("1");
  const [time, setTime] = useState("10:00");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const sessionEnded = useRef(onSessionEnded);
  useEffect(() => { sessionEnded.current = onSessionEnded; }, [onSessionEnded]);
  const adopt = (state: ReminderPreference) => {
    setSaved(state); setEnabled(state.enabled); setDays(String(state.days_before)); setTime(hhmm(state.local_time));
  };
  useEffect(() => {
    let live = true;
    fetch(`${config.apiBaseUrl}/v1/client/reminder-preference`, {
      headers: { Authorization: `Bearer ${token}` }, credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
    }).then(async (response) => {
      if (!live) return;
      if (response.status === 401 || response.status === 403) { sessionEnded.current(); return; }
      if (!response.ok) throw new Error("read failed");
      const state = parsePreference(await response.json());
      if (live) { adopt(state); setError(null); setProblem(null); }
    }).catch(() => { if (live) setError("Your reminder preference could not load. Reload saved preference to try again."); });
    return () => { live = false; };
  }, [config.apiBaseUrl, token, attempt]);

  const invalid = !/^[1-7]$/.test(days) || !TIME_PATTERN.test(time);
  const dirty = saved !== null && (enabled !== saved.enabled || Number(days) !== saved.days_before
    || time !== hhmm(saved.local_time));
  const locked = !saved || busy || Boolean(error);
  const zone = saved ? saved.timezone ?? "Business timezone (not configured)" : "Loading…";

  return <section className="card stack">
    <h2>Visit reminders</h2>
    <p>Choose whether to receive a text reminder before confirmed visits, and when it is sent.</p>
    <p>Saving does not verify your phone, grant SMS consent, or clear STOP. Contact the business if your phone or consent needs updating.</p>
    <form className="stack" aria-label="Visit reminder preference" onSubmit={async (event) => {
      event.preventDefault();
      if (!saved || busy || error || invalid || !dirty) return;
      setBusy(true); setMessage(null); setProblem(null);
      const body: PreferenceBody = { expected_version: saved.version,
        ...(enabled !== saved.enabled ? { enabled } : {}),
        ...(Number(days) !== saved.days_before ? { days_before: Number(days) } : {}),
        ...(time !== hhmm(saved.local_time) ? { local_time: time } : {}),
      };
      try {
        const response = await fetch(`${config.apiBaseUrl}/v1/client/reminder-preference`, {
          method: "PUT", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify(body),
          credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
        });
        if (response.status === 401 || response.status === 403) { sessionEnded.current(); return; }
        if (response.status === 409) {
          setError("Your profile changed. Reload saved preference before saving again."); return;
        }
        if (response.status === 422) {
          setProblem("The business cannot accept those values. Choose 1–7 calendar days and a time from 08:00 through 18:00."); return;
        }
        if (!response.ok) throw new Error("save failed");
        adopt(parsePreference(await response.json())); setMessage("Reminder preference saved.");
      } catch { setError("Could not confirm your save. Reload saved preference before trying again."); }
      finally { setBusy(false); }
    }}>
      <label><input type="checkbox" checked={enabled} disabled={locked}
        onChange={(event) => { setEnabled(event.target.checked); setMessage(null); }} />Receive visit reminders</label>
      <fieldset disabled={locked} className="stack">
        <legend>Reminder timing</legend>
        <p>Times use the business timezone: {zone}.</p>
        <label>Calendar days before<input type="number" name="days_before" min="1" max="7" step="1" required
          value={days} onInput={(event) => { setDays(event.currentTarget.value); setMessage(null); }} /></label>
        <label>Local send time<input type="time" name="local_time" min="08:00" max="18:00" step="60" required
          value={time} onInput={(event) => { setTime(event.currentTarget.value); setMessage(null); }} /></label>
        <p className="hint">Choose 1–7 calendar days before, from 8 a.m. through 6 p.m. Changing timing alone keeps your enrollment unchanged.
          Turning reminders off keeps your saved timing. Missed or failed reminders are not retried.</p>
      </fieldset>
      {saved && <p>Saved: {saved.enabled ? "Enrolled" : "Reminders off"} · {saved.days_before} calendar {saved.days_before === 1 ? "day" : "days"} before at {hhmm(saved.local_time)} · {zone}</p>}
      {saved?.blockers.length ? <div><p>Reminders cannot currently be sent:</p><ul>
        {saved.blockers.map((reason) => <li key={reason}>{reminderRestriction[reason] ?? "SMS delivery is restricted."}</li>)}
      </ul></div> : saved && <p>Your current profile and SMS consent allow reminders when enrolled.</p>}
      {invalid && <p role="alert">Choose a whole number from 1–7 days and a time from 08:00 through 18:00.</p>}
      {problem && <p role="alert">{problem}</p>}
      {error && <p role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
      {!saved && !error && <p role="status">Loading reminder preference…</p>}
      <button type="submit" disabled={locked || invalid || !dirty}>Save reminder preference</button>
      <button type="button" disabled={busy} onClick={() => { setSaved(null); setMessage(null); setError(null); setProblem(null); setAttempt((value) => value + 1); }}>Reload saved preference</button>
    </form>
  </section>;
}

function parsePreference(value: unknown): ReminderPreference {
  const state = value as Partial<ReminderPreference> | null;
  if (!state || typeof state.enabled !== "boolean" || !Number.isInteger(state.version)
    || !Number.isInteger(state.days_before) || typeof state.local_time !== "string"
    || !Array.isArray(state.blockers) || !state.blockers.every((reason) => typeof reason === "string")) {
    throw new Error("Unexpected reminder preference response");
  }
  return state as ReminderPreference;
}
