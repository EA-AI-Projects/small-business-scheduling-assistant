import { useCallback, useEffect, useState } from "react";

import { MiniCalendar } from "@/components/shell/MiniCalendar";
import { bookingState, parseBookings, parseStarts, type ClientBooking } from "@/lib/clientBookings";
import type { OwnerConfig } from "@/lib/config";
import { dayKey, localStamp, localTime, todayKey } from "@/lib/time";

const DURATIONS = [60, 90, 120, 150, 180];

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

function browserZone(): string { return Intl.DateTimeFormat().resolvedOptions().timeZone; }

/** Read-only client calendar and own bookings. Selecting a time does not submit anything yet. */
export function ClientHome({ config, token, onSessionEnded }: {
  config: OwnerConfig; token: string; onSessionEnded: () => void;
}) {
  const zone = browserZone();
  const [date, setDate] = useState(() => todayKey(zone));
  const [duration, setDuration] = useState(120);
  const [bookings, setBookings] = useState<ClientBooking[] | null>(null);
  const [bookingsError, setBookingsError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [chosen, setChosen] = useState<string | null>(null);

  const fail = useCallback((error: unknown, set: (message: string) => void) => {
    if (error instanceof Unauthorized) onSessionEnded();
    else set(error instanceof Error ? error.message : "Something went wrong.");
  }, [onSessionEnded]);

  const [refresh, setRefresh] = useState(0);
  const [availability, setAvailability] = useState<{ key: string; starts: string[] | null; error: string | null } | null>(null);
  const availabilityKey = `${date}|${duration}`;

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

  useEffect(() => {
    let current = true;
    const key = `${date}|${duration}`;
    read(config, token, `/v1/client/availability?day=${date}&duration_minutes=${duration}`)
      .then((data) => { if (current) setAvailability({ key, starts: parseStarts(data), error: null }); })
      .catch((error: unknown) => { if (current) fail(error, (message) => setAvailability({ key, starts: null, error: message })); });
    return () => { current = false; };
  }, [config, token, date, duration, fail]);

  const settled = availability?.key === availabilityKey ? availability : null;
  const starts = settled?.starts ?? null;
  const startsError = settled?.error ?? null;

  const shown = (starts ?? []).filter((start) => dayKey(start, zone) === date);

  return <div className="stack client-home">
    <section className="card" aria-labelledby="client-times">
      <h2 id="client-times">Available times</h2>
      <p className="notice">Choosing a time asks the owner for approval. A time shown here is open, not booked,
        and a request is not a confirmed appointment until the owner approves it.</p>
      <MiniCalendar date={date} today={todayKey(zone)} view="day" onPick={(day) => { setChosen(null); setDate(day); }} />
      <label>Visit length
        <select value={duration} onChange={(event) => { setChosen(null); setDuration(Number(event.target.value)); }}>
          {DURATIONS.map((minutes) => <option key={minutes} value={minutes}>{minutes} minutes</option>)}
        </select>
      </label>
      <p className="meta">Times are shown in your device time zone ({zone}).</p>
      {startsError ? <p className="notice error" role="alert">{startsError}</p>
        : starts === null ? <p>Loading times…</p>
        : shown.length === 0 ? <p>No times are available on this day. Try another day.</p>
        : <ul className="card-list" aria-label="Available start times">
          {shown.map((start) => <li key={start}>
            <button type="button" aria-pressed={chosen === start} onClick={() => setChosen(start)}>{localTime(start, zone)}</button>
          </li>)}
        </ul>}
      {chosen && <p role="status" className="notice">You picked {localStamp(chosen, zone)}. Requesting a time is
        not available in the app yet; nothing has been sent or held. Text the business to request it. Any request
        needs owner approval before it is confirmed.</p>}
    </section>

    <section className="card" aria-labelledby="client-bookings">
      <h2 id="client-bookings">Your appointments</h2>
      <button type="button" onClick={() => setRefresh((count) => count + 1)}>Refresh</button>
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
