import { useEffect, useState } from "react";
import type { ReminderPreferenceBody } from "@/api/types";
import { ApiError, path } from "@/lib/api";
import { reminderRestriction, type ReminderPreference } from "@/lib/reminders";
import { useOwner } from "@/owner/OwnerContext";

export function ReminderPreferenceForm({ clientId, version }: { clientId: string; version: number }) {
  const { api, refresh } = useOwner();
  const [saved, setSaved] = useState<ReminderPreference | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [days, setDays] = useState("1");
  const [time, setTime] = useState("10:00");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    api.get<ReminderPreference>(path`/clients/${clientId}/reminder-preference`).then((state) => {
      if (live) {
        setSaved(state); setEnabled(state.enabled); setDays(String(state.days_before));
        setTime(state.local_time.slice(0, 5)); setError(null);
      }
    }, () => { if (live) setError("Reminder preference could not load. Reload saved preference."); });
    return () => { live = false; };
  }, [api, clientId, version, attempt]);
  const invalid = !Number.isInteger(Number(days)) || Number(days) < 1 || Number(days) > 7
    || !/^(?:0[89]|1[0-7]):[0-5][0-9]$|^18:00$/.test(time);
  const dirty = saved !== null && (enabled !== saved.enabled || Number(days) !== saved.days_before
    || time !== saved.local_time.slice(0, 5));
  const locked = !saved || busy || Boolean(error);
  return <form className="stack" aria-label="Visit reminder preference" onSubmit={async (event) => {
    event.preventDefault();
    if (!saved || busy || error || invalid || !dirty) return;
    setBusy(true); setMessage(null);
    const body: ReminderPreferenceBody = { expected_version: saved.version,
      ...(enabled !== saved.enabled ? { enabled } : {}),
      ...(Number(days) !== saved.days_before ? { days_before: Number(days) } : {}),
      ...(time !== saved.local_time.slice(0, 5) ? { local_time: time } : {}),
    };
    try {
      const result = await api.request<ReminderPreference>(path`/clients/${clientId}/reminder-preference`, {
        method: "PUT", body });
      setSaved(result); setEnabled(result.enabled); setDays(String(result.days_before));
      setTime(result.local_time.slice(0, 5)); setMessage("Reminder preference saved.");
      await refresh();
    } catch (failure) {
      setError(failure instanceof ApiError && failure.status === 409
        ? "This profile changed. Reload saved preference before saving again."
        : "Preference could not be saved. Reload saved preference before trying again.");
    } finally { setBusy(false); }
  }}>
    <p>Record the client’s expressed choice. Enrollment does not give SMS consent, verify a phone, or clear STOP.</p>
    <label><input type="checkbox" checked={enabled} disabled={locked}
      onChange={(event) => { setEnabled(event.target.checked); setMessage(null); }} />Receive visit reminders</label>
    <fieldset disabled={locked} className="stack">
      <legend>Reminder timing</legend>
      <p>Times use the current business timezone: {saved ? saved.timezone ?? "Business timezone (not configured)" : "Loading…"}.</p>
      <label>Calendar days before<input type="number" name="days_before" min="1" max="7" step="1" required
        value={days} onInput={(event) => { setDays(event.currentTarget.value); setMessage(null); }} /></label>
      <label>Local send time<input type="time" name="local_time" min="08:00" max="18:00" step="60" required
        value={time} onInput={(event) => { setTime(event.currentTarget.value); setMessage(null); }} /></label>
      <p className="hint">Choose 1–7 calendar days before, from 8 a.m. through 6 p.m. Changing timing alone keeps enrollment unchanged.
        Turning reminders off keeps the saved timing. Missed or failed reminders are not retried.</p>
    </fieldset>
    {saved && <p>Saved: {saved.enabled ? "Enrolled" : "Reminders off"} · {saved.days_before} calendar {saved.days_before === 1 ? "day" : "days"} before at {saved.local_time.slice(0, 5)} · {saved.timezone ?? "Business timezone (not configured)"}</p>}
    {saved?.blockers?.length ? <div><p>Reminders cannot currently be sent:</p><ul>
      {saved.blockers.map((reason) => <li key={reason}>{reminderRestriction[reason] ?? "SMS delivery is restricted."}</li>)}
    </ul></div> : saved && <p>Current profile and SMS consent allow reminders when enrolled.</p>}
    {saved?.timezone && saved.upcoming_reminders?.length ? <div><p>Expected timing for confirmed visits, using saved preferences. Delivery is not guaranteed.</p><ul>
      {saved.upcoming_reminders.map((reminder) => <li key={reminder.appointment_id}>
        Visit {formatTime(reminder.start_at, saved.timezone!)}: {reminder.status === "disabled" ? "Reminders are off."
          : reminder.status === "blocked" ? "Delivery is currently blocked."
          : reminder.status === "past_time" ? "The reminder time has passed; no catch-up reminder."
          : reminder.status === "attempted" ? "Already attempted; no repeat reminder."
          : `Reminder expected ${formatTime(reminder.run_at, saved.timezone!)}.`}
      </li>)}
    </ul></div> : saved && <p>{saved.timezone ? "No upcoming confirmed visits." : "Expected reminder timing is unavailable until the business timezone is configured."}</p>}
    {invalid && <p role="alert">Choose an integer from 1–7 days and a minute from 08:00 through 18:00.</p>}
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {!saved && !error && <p role="status">Loading reminder preference…</p>}
    <button type="submit" disabled={locked || invalid || !dirty}>Save reminder preference</button>
    <button type="button" disabled={busy} onClick={() => {
      setSaved(null); setMessage(null); setError(null); setAttempt((value) => value + 1);
    }}>Reload saved preference</button>
  </form>;
}

function formatTime(value: string, timezone: string) {
  return new Intl.DateTimeFormat("en-US", { timeZone: timezone, dateStyle: "medium", timeStyle: "short" })
    .format(new Date(value));
}
