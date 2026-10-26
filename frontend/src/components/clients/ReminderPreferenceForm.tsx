import { useEffect, useState } from "react";
import type { ReminderPreferenceBody } from "@/api/types";
import { ApiError, path } from "@/lib/api";
import { reminderRestriction, type ReminderPreference } from "@/lib/reminders";
import { useOwner } from "@/owner/OwnerContext";

export function ReminderPreferenceForm({ clientId, version }: { clientId: string; version: number }) {
  const { api, refresh } = useOwner();
  const [saved, setSaved] = useState<ReminderPreference | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    api.get<ReminderPreference>(path`/clients/${clientId}/reminder-preference`).then((state) => {
      if (live) { setSaved(state); setEnabled(state.enabled); setError(null); }
    }, () => { if (live) setError("Reminder preference could not load. Reload saved preference."); });
    return () => { live = false; };
  }, [api, clientId, version, attempt]);
  return <form className="stack" onSubmit={async (event) => {
    event.preventDefault();
    if (!saved || busy) return;
    setBusy(true); setError(null); setMessage(null);
    try {
      const result = await api.request<ReminderPreference>(path`/clients/${clientId}/reminder-preference`, {
        method: "PUT", body: { enabled, expected_version: saved.version } satisfies ReminderPreferenceBody });
      setSaved(result); setMessage("Reminder preference saved.");
      await refresh();
    } catch (failure) {
      setError(failure instanceof ApiError && failure.status === 409
        ? "This profile changed. Reload saved preference before saving again."
        : "Preference could not be saved. Reload saved preference before trying again.");
    } finally { setBusy(false); }
  }}>
    <p>Record the client’s expressed choice. Enrollment does not give SMS consent, verify a phone, or clear STOP.</p>
    <label><input type="checkbox" checked={enabled} disabled={!saved || busy}
      onChange={(event) => setEnabled(event.target.checked)} />Receive visit reminders</label>
    {saved?.blockers?.length ? <div><p>Reminders cannot currently be sent:</p><ul>
      {saved.blockers.map((reason) => <li key={reason}>{reminderRestriction[reason] ?? "SMS delivery is restricted."}</li>)}
    </ul></div> : saved && <p>Current profile and SMS consent allow reminders when enrolled.</p>}
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    <button type="submit" disabled={!saved || busy || saved.enabled === enabled}>Save reminder preference</button>
    {error && <button type="button" disabled={busy} onClick={() => setAttempt((value) => value + 1)}>
      Reload saved preference</button>}
  </form>;
}
