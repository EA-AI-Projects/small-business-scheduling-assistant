import { useCallback, useEffect, useRef, useState } from "react";

import { MiniCalendar } from "@/components/shell/MiniCalendar";
import { bookingState, hasStalePending, nextHoldExpiry, parseAvailability, parseBookings, type ClientBooking } from "@/lib/clientBookings";
import type { OwnerConfig } from "@/lib/config";
import { dayKey, localStamp, localTime, todayKey } from "@/lib/time";

export const RETRY_BASE_MS = 2000;
const MAX_RETRIES = 4;

class Unauthorized extends Error {}

async function read(config: OwnerConfig, token: string, path: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${config.apiBaseUrl}${path}`, { headers: { Authorization: `Bearer ${token}` },
      credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer" });
  } catch { throw new Error("Could not reach the scheduling service. Please try again."); }
  if (response.status === 401 || response.status === 403) throw new Unauthorized();
  if (response.status === 503) throw new Error("Online times are not available right now.");
  if (!response.ok) throw new Error("Something went wrong. Please try again.");
  return response.json().catch(() => null);
}

type Outcome =
  | { kind: "sent"; booking: ClientBooking }
  | { kind: "conflict"; alternatives: string[] }
  | { kind: "error"; message: string };

/** Submit one request. The body names only the start; the server fixes client, business, and length. */
async function submitRequest(config: OwnerConfig, token: string, start: string, key: string): Promise<Outcome> {
  let response: Response;
  try {
    response = await fetch(`${config.apiBaseUrl}/v1/client/requests`, { method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({ start_at: start }),
      credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer" });
  } catch { return { kind: "error", message: "Could not tell whether your request was sent. Check Your appointments, "
    + "or send it again; sending again will not create a second request." }; }
  if (response.status === 401 || response.status === 403) throw new Unauthorized();
  const body = await response.json().catch(() => null) as { detail?: { code?: string; alternatives?: unknown } } | null;
  if (response.ok) {
    const list = parseBookings({ bookings: [body] });
    const [booking] = list;
    return list.length === 1 && booking ? { kind: "sent", booking }
      : { kind: "error", message: "Unexpected response. Check Your appointments before trying again." };
  }
  if (response.status === 409 && (body?.detail?.code === "SLOT_CONFLICT" || body?.detail?.code === "CALENDAR_BUSY")) {
    const alternatives = Array.isArray(body.detail.alternatives)
      ? body.detail.alternatives.filter((item): item is string => typeof item === "string" && !Number.isNaN(Date.parse(item))) : [];
    return { kind: "conflict", alternatives };
  }
  if (response.status === 409 && body?.detail?.code === "PROFILE_INCOMPLETE") return { kind: "error",
    message: "Your profile needs the owner's attention before you can request online. Please contact the business." };
  if (response.status === 503) return { kind: "error", message: "Online requests are not available right now." };
  return { kind: "error", message: "Your request was not sent. Please try again." };
}

/**
 * Client calendar and own bookings, in the business time zone. Choosing a time and confirming sends a
 * request for owner approval; it is never an appointment until the owner approves. The visit length is
 * the owner-set length on the client's profile, chosen by the server.
 */
export function ClientHome({ config, token, zone, onSessionEnded }: {
  config: OwnerConfig; token: string; zone: string | null; onSessionEnded: () => void;
}) {
  if (!zone) return <section className="card"><p className="notice error" role="alert">
    Online times are not available right now. Please text the business.</p></section>;
  return <ClientCalendar config={config} token={token} zone={zone} onSessionEnded={onSessionEnded} />;
}

function ClientCalendar({ config, token, zone, onSessionEnded }: {
  config: OwnerConfig; token: string; zone: string; onSessionEnded: () => void;
}) {
  const [date, setDate] = useState(() => todayKey(zone));
  const [bookings, setBookings] = useState<ClientBooking[] | null>(null);
  const [bookingsError, setBookingsError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [chosen, setChosen] = useState<string | null>(null);
  // One key per chosen time, reused on a retry so a lost response cannot create a second request.
  const attempt = useRef<{ start: string; key: string } | null>(null);
  const [sending, setSending] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [availability, setAvailability] = useState<{ key: string; starts: string[] | null;
    minutes: number | null; error: string | null } | null>(null);
  const availabilityKey = `${date}|${refresh}`;

  const fail = useCallback((error: unknown, set: (message: string) => void) => {
    if (error instanceof Unauthorized) onSessionEnded();
    else set(error instanceof Error ? error.message : "Something went wrong.");
  }, [onSessionEnded]);

  useEffect(() => {
    let current = true;
    read(config, token, "/v1/client/bookings").then((data) => {
      if (!current) return;
      setBookings(parseBookings(data)); setBookingsError(null); setNowMs(Date.now());
    }).catch((error: unknown) => { if (current) fail(error, setBookingsError); });
    return () => { current = false; };
  }, [config, token, fail, refresh]);
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  // Re-read when a pending hold passes so the list reflects the server, not only the device clock.
  // If the server still says pending after the device clock passes the hold, keep asking a few times
  // with backoff; only the server can say the request expired.
  const retries = useRef(0);
  useEffect(() => {
    if (!bookings) return;
    const now = Date.now();
    let delay: number;
    if (hasStalePending(bookings, now)) {
      if (retries.current >= MAX_RETRIES) return;
      delay = RETRY_BASE_MS * 2 ** retries.current;
      retries.current += 1;
    } else {
      retries.current = 0;
      const expiry = nextHoldExpiry(bookings, now);
      if (expiry === null) return;
      delay = expiry - now + 250;
    }
    const timer = window.setTimeout(() => setRefresh((count) => count + 1), delay);
    return () => window.clearTimeout(timer);
  }, [bookings]);

  useEffect(() => {
    let current = true;
    const key = availabilityKey;
    read(config, token, `/v1/client/availability?day=${date}`)
      .then((data) => {
        if (!current) return;
        const parsed = parseAvailability(data);
        setAvailability({ key, starts: parsed.starts, minutes: parsed.durationMinutes, error: null });
      })
      .catch((error: unknown) => { if (current) fail(error, (message) =>
        setAvailability({ key, starts: null, minutes: null, error: message })); });
    return () => { current = false; };
  }, [config, token, date, availabilityKey, fail]);

  const choose = (start: string | null) => { setChosen(start); setOutcome(null); };
  const send = async () => {
    if (!chosen || sending) return;
    if (attempt.current?.start !== chosen) attempt.current = { start: chosen, key: crypto.randomUUID() };
    setSending(true);
    try {
      const result = await submitRequest(config, token, chosen, attempt.current.key);
      if (result.kind !== "error") { attempt.current = null; setChosen(null); setRefresh((count) => count + 1); }
      setOutcome(result);
    } catch (error) { fail(error, (message) => setOutcome({ kind: "error", message })); }
    finally { setSending(false); }
  };

  const settled = availability?.key === availabilityKey ? availability : null;
  const starts = settled?.starts ?? null;
  const startsError = settled?.error ?? null;
  const shown = (starts ?? []).filter((start) => dayKey(start, zone) === date);

  return <div className="stack client-home">
    <section className="card" aria-labelledby="client-times">
      <h2 id="client-times">Available times</h2>
      <p className="notice">Choosing a time asks the owner for approval. A time shown here is open, not booked,
        and a request is not a confirmed appointment until the owner approves it.</p>
      <MiniCalendar date={date} today={todayKey(zone)} view="day"
        onPick={(day) => { choose(null); setDate(day); }} />
      <p className="meta">Times are shown in the business time zone ({zone}).
        {settled?.minutes ? ` Visits are about ${settled.minutes} minutes.` : ""}</p>
      {startsError ? <p className="notice error" role="alert">{startsError}</p>
        : starts === null ? <p>Loading times…</p>
        : shown.length === 0 ? <p>No times are available on this day. Try another day.</p>
        : <ul className="card-list" aria-label="Available start times">
          {shown.map((start) => <li key={start}>
            <button type="button" aria-pressed={chosen === start} onClick={() => choose(start)}>{localTime(start, zone)}</button>
          </li>)}
        </ul>}
      {chosen && <div role="group" aria-label="Request this time" className="notice">
        <p>You picked <strong>{localStamp(chosen, zone)}</strong> ({zone}). Sending asks the owner for approval.
          This time is not booked and is not confirmed until the owner approves it.</p>
        <button type="button" disabled={sending} onClick={() => void send()}>
          {sending ? "Sending…" : "Send request for owner approval"}</button>
      </div>}
      {outcome?.kind === "sent" && <p role="status" className="notice">Request sent for{" "}
        <strong>{localStamp(outcome.booking.start_at, zone)}</strong> to {localTime(outcome.booking.end_at, zone)}.
        Status: waiting for owner approval. It is not confirmed yet.</p>}
      {outcome?.kind === "conflict" && <div role="alert" className="notice error">
        <p>That time is no longer open, so nothing was requested.
          {outcome.alternatives.length ? " These times are open now:" : " Please pick another time or day."}</p>
        {outcome.alternatives.length > 0 && <ul className="card-list" aria-label="Other open times">
          {outcome.alternatives.map((start) => <li key={start}>
            <button type="button" onClick={() => { setDate(dayKey(start, zone)); choose(start); }}>{localStamp(start, zone)}</button>
          </li>)}</ul>}
      </div>}
      {outcome?.kind === "error" && <p role="alert" className="notice error">{outcome.message}</p>}
    </section>

    <section className="card" aria-labelledby="client-bookings">
      <h2 id="client-bookings">Your appointments</h2>
      <button type="button" onClick={() => { choose(null); setRefresh((count) => count + 1); }}>Refresh</button>
      {bookingsError ? <p className="notice error" role="alert">{bookingsError}</p>
        : bookings === null ? <p>Loading your appointments…</p>
        : bookings.length === 0 ? <p>You have no upcoming appointments or pending requests.</p>
        : <ul className="card-list">{bookings.map((booking) => {
          const state = bookingState(booking, nowMs);
          return <li key={booking.appointment_id} className="card" data-status={state.tone}>
            <strong>{localStamp(booking.start_at, zone)}</strong> to {localTime(booking.end_at, zone)}{" "}
            <span className="badge">{state.label}</span>
            <p className="meta">{state.detail}</p>
          </li>;
        })}</ul>}
    </section>
  </div>;
}
