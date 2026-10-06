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

export function ManualInvitationForm() {
  const { api, data, notify } = useOwner();
  const storageKey = `manual-booking-invitation-pending:${api.businessId}`;
  const [message, setMessage] = useState(() => savedAttempt(storageKey)?.message ?? "");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [pending, setPending] = useState(() => savedAttempt(storageKey));

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
    if (!preview?.delivery_enabled || !text || !text.toUpperCase().includes("STOP")) return;
    const count = preview.eligible;
    if (!pending && !window.confirm(`Queue this message for ${count} eligible client${count === 1 ? "" : "s"}?`)) return;
    const attempt: PendingAttempt = pending ?? { key: crypto.randomUUID(), message: text,
      cursor: null, queued: 0, createdAt: Date.now() };
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
    } catch (error) {
      notify(`Send status uncertain. Retry uses the same run and will not duplicate queued clients. ${
        error instanceof Error ? error.message : String(error)}`, true);
    }
  }

  return <section className="card outreach-card">
    <h3>Send invitations now</h3>
    <p className="hint">Send one custom booking invitation to every eligible client in the saved
      lookahead window. Manual sends have no repeat limit. Consent, opt-outs, verified numbers,
      and confirmed appointments are checked again before delivery.</p>
    <p className="hint">Custom-message delivery becomes available after the separate Twilio
      campaign and live rollout are authorized.</p>
    <label>Message
      <textarea value={message} maxLength={500} rows={5} disabled={pending !== null}
        onChange={(event) => setMessage(event.target.value)}
        placeholder="Smart Scheduling Assistant: Would you like to book a cleaning visit? Reply with a day and time, or STOP to opt out." />
    </label>
    <p className="meta">{preview
      ? `${preview.eligible} of ${preview.examined} clients currently eligible`
      : "Save a lookahead window to preview eligible clients."}</p>
    {!preview?.delivery_enabled && <p className="hint">Manual delivery is not enabled yet.</p>}
    <BusyButton className="primary" onClick={send} disabled={!preview?.delivery_enabled
      || (!preview.eligible && !pending) || !message.trim()
      || !message.toUpperCase().includes("STOP")}>{pending ? "Retry same send" : "Send invitations"}</BusyButton>
    {result && <p role="status">{result}</p>}
  </section>;
}
