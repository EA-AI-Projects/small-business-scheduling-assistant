import { useEffect, useState } from "react";

import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";

interface Preview {
  eligible: number;
  examined: number;
  delivery_enabled: boolean;
}

interface Result {
  queued: number;
  already_queued: number;
  next_cursor: string | null;
}

interface PendingAttempt {
  key: string;
  message: string;
  cursor: string | null;
  queued: number;
  createdAt: number;
}

function savedAttempt(storageKey: string): PendingAttempt | null {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
    if (value && typeof value === "object" && "key" in value && "message" in value
        && "cursor" in value && "queued" in value && "createdAt" in value
        && typeof value.key === "string" && typeof value.message === "string"
        && (value.cursor === null || typeof value.cursor === "string")
        && typeof value.queued === "number" && typeof value.createdAt === "number"
        && Date.now() - value.createdAt < 14 * 24 * 60 * 60 * 1000) {
      return { key: value.key, message: value.message, cursor: value.cursor,
        queued: value.queued, createdAt: value.createdAt };
    }
    sessionStorage.removeItem(storageKey);
  } catch { /* Storage can be unavailable in an embedded browser. */ }
  return null;
}

export function ManualInvitationForm({ message, onMessageChange, prepareSend, lookaheadWeeks }: {
  message: string;
  onMessageChange: (value: string) => void;
  prepareSend: (key: string) => Promise<boolean>;
  lookaheadWeeks: number | null;
}) {
  const { api, data, notify, refresh } = useOwner();
  const storageKey = `manual-booking-invitation-pending:${api.businessId}`;
  const [pending, setPending] = useState(() => savedAttempt(storageKey));
  const [preview, setPreview] = useState<Preview | null>(null);
  const [result, setResult] = useState<string | null>(null);

  useEffect(() => {
    if (pending) onMessageChange(pending.message);
  }, [pending, onMessageChange]);

  useEffect(() => {
    let active = true;
    if (!data.outreach?.settings.lookahead_weeks) return;
    api.get<Preview>("/booking-invitations/manual")
      .then((value) => { if (active) setPreview(value); })
      .catch(() => { if (active) setPreview(null); });
    return () => { active = false; };
  }, [api, data.outreach?.settings.lookahead_weeks]);

  async function send() {
    const text = pending?.message ?? message.trim();
    if (!text || (!lookaheadWeeks && !pending)) return;
    let attempt = pending;
    if (!attempt) {
      const key = crypto.randomUUID();
      if (!await prepareSend(key)) return;
      let current: Preview;
      try {
        current = await api.get<Preview>("/booking-invitations/manual");
        setPreview(current);
      } catch (error) {
        notify(error instanceof Error ? error.message : String(error), true);
        return;
      }
      if (!current.delivery_enabled || current.eligible === 0) {
        notify("Manual delivery is unavailable or no clients are currently eligible", true);
        return;
      }
      const count = current.eligible;
      if (!window.confirm(`Queue this message for ${count} eligible client${count === 1 ? "" : "s"}?`)) return;
      attempt = { key, message: text, cursor: null, queued: 0, createdAt: Date.now() };
    }
    setPending(attempt);
    try { sessionStorage.setItem(storageKey, JSON.stringify(attempt)); } catch { /* Best effort. */ }
    try {
      let current = attempt;
      while (true) {
        const response = await api.request<Result>("/booking-invitations/manual", {
          method: "POST", body: { message: current.message, cursor: current.cursor },
          idempotencyKey: current.key,
        });
        current = { ...current, cursor: response.next_cursor,
          queued: current.queued + response.queued + response.already_queued };
        setPending(current);
        try { sessionStorage.setItem(storageKey, JSON.stringify(current)); } catch { /* Best effort. */ }
        if (response.next_cursor === null) break;
      }
      const total = current.queued;
      const summary = `${total} invitation${total === 1 ? "" : "s"} queued for this run`;
      setResult(summary);
      setPending(null);
      try { sessionStorage.removeItem(storageKey); } catch { /* Best effort. */ }
      notify(summary);
      api.get<Preview>("/booking-invitations/manual")
        .then(setPreview).catch(() => setPreview(null));
      try { await refresh(); } catch {
        notify(`${summary}, but the latest settings could not load`, true);
      }
    } catch (error) {
      notify(`Send status uncertain. Retry uses the same run and will not duplicate queued clients. ${
        error instanceof Error ? error.message : String(error)}`, true);
    }
  }

  return <div className="form-stack">
    <label>Invitation message
      <textarea value={message} maxLength={500} rows={5} disabled={pending !== null}
        onChange={(event) => onMessageChange(event.target.value)}
        placeholder="Write an invitation message." />
    </label>
    <p className="hint">This message is used for weekly invitations and Send Invitations Now.
      Sending now saves changes first and has no repeat limit.</p>
    <p className="meta">{preview
      ? `${preview.eligible} of ${preview.examined} clients currently eligible`
      : "Save a lookahead window to preview eligible clients."}</p>
    {!preview?.delivery_enabled && <p className="hint">Manual delivery is not enabled yet.</p>}
    <div><BusyButton className="primary" onClick={send} disabled={
      (!message.trim() && !pending) || (!lookaheadWeeks && !pending)
      || preview?.delivery_enabled === false}>{pending ? "Retry same send" : "Send Invitations Now"}</BusyButton></div>
    {result && <p role="status">{result}</p>}
  </div>;
}
