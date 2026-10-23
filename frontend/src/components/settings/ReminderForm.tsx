import { useEffect, useState } from "react";
import type { ReminderEditBody } from "@/api/types";
import { ApiError } from "@/lib/api";
import { useOwner } from "@/owner/OwnerContext";

export interface ReminderState {
  version: number;
  settings: { days_before: number; local_time: string };
}

export function ReminderForm({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) {
  const { api, data } = useOwner();
  const [saved, setSaved] = useState<ReminderState | null>(null);
  const [days, setDays] = useState("1");
  const [time, setTime] = useState("10:00");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    api.get<ReminderState>("/reminders").then((state) => {
      if (live) { setSaved(state); setDays(String(state.settings.days_before));
        setTime(state.settings.local_time.slice(0, 5)); setError(null); }
    }, () => { if (live) setError("Reminder settings could not load. Reload saved settings."); });
    return () => { live = false; };
  }, [api, attempt]);
  const dirty = saved !== null && (Number(days) !== saved.settings.days_before
    || time !== saved.settings.local_time.slice(0, 5));
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  const invalid = !Number.isInteger(Number(days)) || Number(days) < 1 || Number(days) > 7
    || !/^\d{2}:\d{2}$/.test(time) || time < "08:00" || time > "18:00";
  return <form className="stack" onSubmit={async (event) => {
    event.preventDefault();
    if (!saved || invalid || busy) return;
    setBusy(true); setError(null); setMessage(null);
    try {
      const result = await api.request<ReminderState>("/reminders", { method: "PUT", idempotent: true,
        body: { expected_version: saved.version, settings: { days_before: Number(days), local_time: time } } satisfies ReminderEditBody });
      setSaved(result); setMessage("Reminder timing saved.");
    } catch (failure) {
      setError(failure instanceof ApiError && failure.status === 409
        ? "Reminder settings changed. Reload saved settings before saving again."
        : "Reminder timing could not be saved. Reload saved settings before trying again.");
    } finally { setBusy(false); }
  }}>
    <p>One text per enrolled client’s confirmed visit. Times use {data.zone}.</p>
    <label>Calendar days before<input type="number" min="1" max="7" step="1" required
      value={days} onInput={(event) => setDays(event.currentTarget.value)} /></label>
    <label>Local send time<input type="time" min="08:00" max="18:00" step="60" required
      value={time} onInput={(event) => setTime(event.currentTarget.value)} /></label>
    <p className="hint">Choose 1–7 days before, from 8 a.m. through 6 p.m. Enrollment starts off.
      Missed and failed reminders are not retried.</p>
    {invalid && <p role="alert">Choose 1–7 days and a time from 08:00 through 18:00.</p>}
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    <button type="submit" disabled={!saved || invalid || busy || !dirty}>Save reminder timing</button>
    {error && <button type="button" disabled={busy} onClick={() => setAttempt((value) => value + 1)}>
      Reload saved settings</button>}
  </form>;
}
