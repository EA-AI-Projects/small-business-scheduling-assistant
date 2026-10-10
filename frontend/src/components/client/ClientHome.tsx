import { useCallback, useEffect, useRef, useState } from "react";

import type { CalendarState } from "@/calendar/useCalendarState";
import { MiniCalendar } from "@/components/shell/MiniCalendar";
import { bookingState, calendarItems, hasStalePending, hasVersion, nextHoldExpiry, parseAvailability, parseBookings, pendingReplacement, type ClientBooking } from "@/lib/clientBookings";
import type { OwnerConfig } from "@/lib/config";
import { addDays, dayKey, localStamp, localTime, todayKey } from "@/lib/time";

import { ClientCalendarViews } from "./ClientCalendarViews";

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
  | { kind: "error"; message: string; stale?: boolean };

const REFUSALS: Record<string, string> = {
  STALE_BOOKING: "That appointment changed since you last looked, so nothing was changed. Refresh and review it first.",
  REPLACEMENT_PENDING: "A move request for this appointment is already waiting for the owner. Withdraw it first to change anything else.",
  BOOKING_NOT_ACTIVE: "That appointment is no longer active, so nothing was changed. Refresh to see its current state.",
  BOOKING_NOT_RESCHEDULABLE: "Only an upcoming confirmed appointment can be moved. Refresh to see its current state.",
  CALENDAR_BUSY: "The calendar changed while sending. Nothing was changed; please try again.",
  PROFILE_INCOMPLETE: "Your profile needs the owner's attention before you can change appointments online. Please contact the business.",
  IDEMPOTENCY_KEY_REUSED: "That attempt was already used for something else. Refresh and try again.",
};

/** One client write. The body names only a start and the version the client saw; the server fixes everything else. */
async function write(config: OwnerConfig, token: string, path: string, payload: unknown, key: string, timed: boolean): Promise<Outcome> {
  let response: Response;
  try {
    response = await fetch(`${config.apiBaseUrl}${path}`, { method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify(payload),
      credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer" });
  } catch { return { kind: "error", message: (timed ? "Could not tell whether your request was sent. Check Your appointments, "
      + "or send it again; sending again will not create a second request."
    : "Could not tell whether this was sent. Check Your appointments, "
      + "or press it again; pressing again will not repeat it.") }; }
  if (response.status === 401 || response.status === 403) throw new Unauthorized();
  const body = await response.json().catch(() => null) as { detail?: { code?: string; alternatives?: unknown } } | null;
  if (response.ok) {
    const list = parseBookings({ bookings: [body] });
    const [booking] = list;
    return list.length === 1 && booking ? { kind: "sent", booking }
      : { kind: "error", message: "Unexpected response. Check Your appointments before trying again." };
  }
  const code = body?.detail?.code;
  // Only a chosen time can be "no longer open"; a cancel that hits a busy calendar is a plain retry.
  if (timed && response.status === 409 && (code === "SLOT_CONFLICT" || code === "CALENDAR_BUSY")) {
    const alternatives = Array.isArray(body?.detail?.alternatives)
      ? body.detail.alternatives.filter((item): item is string => typeof item === "string" && !Number.isNaN(Date.parse(item))) : [];
    return { kind: "conflict", alternatives };
  }
  if (response.status === 409 && code && code in REFUSALS) return { kind: "error", message: REFUSALS[code]!,
    stale: code === "STALE_BOOKING" || code === "BOOKING_NOT_ACTIVE" || code === "BOOKING_NOT_RESCHEDULABLE" };
  if (response.status === 404) return { kind: "error", message: "That appointment was not found. Refresh to see your appointments." };
  if (response.status === 503) return { kind: "error", message: timed ? "Online requests are not available right now." : "Online changes are not available right now." };
  return { kind: "error", message: timed ? "Your request was not sent. Please try again."
    : "Nothing was changed. Please try again." };
}

/**
 * Client calendar and own bookings, in the business time zone. Choosing a time and confirming sends a
 * request for owner approval; it is never an appointment until the owner approves. The visit length is
 * the owner-set length on the client's profile, chosen by the server.
 */
export function ClientHome({ config, token, zone, onSessionEnded, calendar, horizonDays }: {
  config: OwnerConfig; token: string; zone: string | null; onSessionEnded: () => void;
  /** Given on the Calendar page: its shared view state drives Day to Year views. Omitted on Appointments. */
  calendar?: CalendarState;
  /** Days ahead the business takes bookings, when the server said. */
  horizonDays?: number | null;
}) {
  if (!zone) return <section className="card"><p className="notice error" role="alert">
    Online times are not available right now. Please text the business.</p></section>;
  return <ClientCalendar config={config} token={token} zone={zone} onSessionEnded={onSessionEnded}
    calendar={calendar} horizonDays={horizonDays ?? null} />;
}

function ClientCalendar({ config, token, zone, onSessionEnded, calendar, horizonDays }: {
  config: OwnerConfig; token: string; zone: string; onSessionEnded: () => void;
  calendar: CalendarState | undefined; horizonDays: number | null;
}) {
  const [ownDate, setOwnDate] = useState(() => todayKey(zone));
  const date = calendar ? calendar.date : ownDate;
  const setDate = calendar ? calendar.goToDate : setOwnDate;
  // The Calendar page asks for open times only in Day view, and never for a past day or past the booking horizon.
  const today = todayKey(zone);
  const beyondHorizon = horizonDays !== null && date > addDays(today, horizonDays);
  const wantTimes = (!calendar || (calendar.view === "day" && date >= today)) && !beyondHorizon;
  const [bookings, setBookings] = useState<ClientBooking[] | null>(null);
  const [bookingsError, setBookingsError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [chosen, setChosen] = useState<string | null>(null);
  // One key per chosen time, reused on a retry so a lost response cannot create a second request.
  const attempt = useRef<{ id: string; key: string } | null>(null);
  // A visit being moved: the confirmed original stays booked until the owner approves the replacement.
  const [heldMoving, setMoving] = useState<ClientBooking | null>(null);
  // A booking whose cancellation is waiting for the separate, explicit confirm press.
  const [heldConfirming, setConfirming] = useState<ClientBooking | null>(null);
  const cancelAttempt = useRef<{ id: string; key: string } | null>(null);
  const [changing, setChanging] = useState(false);
  const [changeOutcome, setChangeOutcome] = useState<{ kind: "cancelled"; booking: ClientBooking }
    | { kind: "error"; message: string } | null>(null);
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
    if (!wantTimes) return;
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
  }, [config, token, date, availabilityKey, fail, wantTimes]);

  // A re-read that changed a booking's version drops any panel still holding the old one.
  const stillCurrent = (held: ClientBooking | null) => held && bookings?.some((item) =>
    item.appointment_id === held.appointment_id && item.version === held.version) ? held : null;
  const moving = stillCurrent(heldMoving);
  const confirming = stillCurrent(heldConfirming);
  const choose = (start: string | null) => { setChosen(start); setOutcome(null); };
  // In calendar mode a new date or view drops any chosen time and outcome, as picking a day always did.
  const calendarView = calendar?.view;
  const lastSeen = useRef({ date, view: calendarView });
  useEffect(() => {
    if (!calendar) return;
    if (lastSeen.current.date === date && lastSeen.current.view === calendarView) return;
    lastSeen.current = { date, view: calendarView };
    setChosen(null); setOutcome(null); attempt.current = null;
  }, [calendar, date, calendarView]);
  const send = async () => {
    if (!chosen || sending) return;
    const id = `${moving?.appointment_id ?? "new"}|${moving?.version ?? 0}|${chosen}`;
    if (attempt.current?.id !== id) attempt.current = { id, key: crypto.randomUUID() };
    setSending(true);
    try {
      const result = moving && hasVersion(moving)
        ? await write(config, token, `/v1/client/bookings/${encodeURIComponent(moving.appointment_id)}/reschedule`,
          { start_at: chosen, expected_version: moving.version }, attempt.current.key, true)
        : await write(config, token, "/v1/client/requests", { start_at: chosen }, attempt.current.key, true);
      if (result.kind !== "error") { attempt.current = null; setChosen(null); setRefresh((count) => count + 1); }
      if (result.kind === "sent" || (result.kind === "error" && result.stale)) {
        // A refused or finished move never leaves an old version in the panel.
        setMoving(null); setChosen(null); attempt.current = null; setRefresh((count) => count + 1);
      }
      setOutcome(result);
    } catch (error) { fail(error, (message) => setOutcome({ kind: "error", message })); }
    finally { setSending(false); }
  };
  const startCancel = (booking: ClientBooking) => {
    setConfirming(booking); setChangeOutcome(null); setMoving(null); choose(null);
  };
  const confirmCancel = async () => {
    if (!confirming || !hasVersion(confirming) || changing) return;
    const id = `${confirming.appointment_id}|${confirming.version}`;
    if (cancelAttempt.current?.id !== id) cancelAttempt.current = { id, key: crypto.randomUUID() };
    setChanging(true);
    try {
      const result = await write(config, token,
        `/v1/client/bookings/${encodeURIComponent(confirming.appointment_id)}/cancel`,
        { expected_version: confirming.version }, cancelAttempt.current.key, false);
      if (result.kind === "sent") {
        cancelAttempt.current = null; setConfirming(null); setChangeOutcome({ kind: "cancelled", booking: result.booking });
        setRefresh((count) => count + 1);
      } else if (result.kind === "error") {
        setChangeOutcome(result);
        if (result.stale) { setConfirming(null); cancelAttempt.current = null; setRefresh((count) => count + 1); }
      }
    } catch (error) { fail(error, (message) => setChangeOutcome({ kind: "error", message })); }
    finally { setChanging(false); }
  };
  const startMove = (booking: ClientBooking) => {
    setMoving(booking); setConfirming(null); setChangeOutcome(null); choose(null);
  };

  const settled = availability?.key === availabilityKey ? availability : null;
  const starts = settled?.starts ?? null;
  const startsError = settled?.error ?? null;
  const shown = (starts ?? []).filter((start) => dayKey(start, zone) === date);

  const timesSection = (
    <section className="card" aria-labelledby="client-times">
        <h2 id="client-times">Available times</h2>
        <p className="notice">Choosing a time asks the owner for approval. A time shown here is open, not booked,
          and a request is not a confirmed appointment until the owner approves it.</p>
        {!calendar && <MiniCalendar date={date} today={todayKey(zone)} view="day"
          onPick={(day) => { choose(null); setDate(day); }} />}
        <p className="meta">Times are shown in the business time zone ({zone}).
          {settled?.minutes ? ` Visits are about ${settled.minutes} minutes.` : ""}</p>
        {beyondHorizon ? <p>No times are available on this day. Try another day.</p>
          : startsError ? <p className="notice error" role="alert">{startsError}</p>
          : starts === null ? <p>Loading times…</p>
          : shown.length === 0 ? <p>No times are available on this day. Try another day.</p>
          : <ul className="card-list" aria-label="Available start times">
            {shown.map((start) => <li key={start}>
              <button type="button" aria-pressed={chosen === start} onClick={() => choose(start)}>{localTime(start, zone)}</button>
            </li>)}
          </ul>}
        {moving && <div role="group" aria-label="Moving this appointment" className="notice">
          <p>You are asking to move your appointment on <strong>{localStamp(moving.start_at, zone)}</strong>.
            Pick a new time above. Your current appointment stays confirmed until the owner approves the new time;
            if the owner declines or does not answer, nothing changes.</p>
          <button type="button" onClick={() => { setMoving(null); choose(null); }}>Keep my current appointment</button>
        </div>}
        {chosen && <div role="group" aria-label="Request this time" className="notice">
          <p>You picked <strong>{localStamp(chosen, zone)}</strong> ({zone}). Sending asks the owner for approval.
            This time is not booked and is not confirmed until the owner approves it.
            {moving ? ` Your appointment on ${localStamp(moving.start_at, zone)} stays confirmed until then.` : ""}</p>
          <button type="button" disabled={sending} onClick={() => void send()}>
            {sending ? "Sending…" : moving ? "Send move request for owner approval" : "Send request for owner approval"}</button>
        </div>}
        {outcome?.kind === "sent" && outcome.booking.status !== "PENDING_APPROVAL" && <p role="status" className="notice">
          This request is no longer waiting for approval: {bookingState(outcome.booking, nowMs).label}.{" "}
          {bookingState(outcome.booking, nowMs).detail}</p>}
        {outcome?.kind === "sent" && outcome.booking.status === "PENDING_APPROVAL" && <p role="status" className="notice">{outcome.booking.replaces_appointment_id ? "Move requested for" : "Request sent for"}{" "}
          <strong>{localStamp(outcome.booking.start_at, zone)}</strong> to {localTime(outcome.booking.end_at, zone)}.
          Status: waiting for owner approval. It is not confirmed yet.
          {outcome.booking.replaces_appointment_id ? " Your original appointment is still confirmed." : ""}</p>}
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
  );
  if (calendar) {
    const items = bookings ? calendarItems(bookings, nowMs) : [];
    return <div className="stack client-home">
      <section className="card" aria-label="Your calendar">
        <p className="meta">Only your own appointments and requests are shown. Times are shown in the business time zone ({zone}).
          A request is not an appointment until the owner approves it.</p>
        <ClientCalendarViews items={items} bookings={bookings ?? []} calendar={calendar} zone={zone} nowMs={nowMs}
          loading={bookings === null && !bookingsError} error={bookingsError} />
      </section>
      {calendar.view === "day" && date >= today && timesSection}
    </div>;
  }
  return <div className="stack client-home">
    {timesSection}

    <section className="card" aria-labelledby="client-bookings">
      <h2 id="client-bookings">Your appointments</h2>
      <button type="button" onClick={() => { choose(null); setConfirming(null); setMoving(null); setRefresh((count) => count + 1); }}>Refresh</button>
      {bookingsError ? <p className="notice error" role="alert">{bookingsError}</p>
        : bookings === null ? <p>Loading your appointments…</p>
        : bookings.length === 0 ? <p>You have no upcoming appointments or pending requests.</p>
        : <ul className="card-list">{bookings.map((booking) => {
          const state = bookingState(booking, nowMs);
          const live = state.tone === "confirmed" || state.label === "Waiting for approval";
          const replacement = booking.status === "CONFIRMED" ? pendingReplacement(bookings, booking) : null;
          const original = booking.replaces_appointment_id
            ? bookings.find((item) => item.appointment_id === booking.replaces_appointment_id) : undefined;
          const canChange = live && hasVersion(booking);
          const isMove = Boolean(booking.replaces_appointment_id) && booking.status === "PENDING_APPROVAL";
          return <li key={booking.appointment_id} className="card" data-status={state.tone}>
            <strong>{localStamp(booking.start_at, zone)}</strong> to {localTime(booking.end_at, zone)}{" "}
            <span className="badge">{booking.replaces_appointment_id && booking.status === "PENDING_APPROVAL"
              ? "Move request: waiting for approval" : state.label}</span>
            <p className="meta">{booking.replaces_appointment_id && booking.status === "PENDING_APPROVAL"
              ? `Not confirmed. This is a request to move${original ? ` your ${localStamp(original.start_at, zone)} appointment` : " your appointment"} here. `
                + "Your original appointment stays confirmed until the owner approves this time."
              : state.detail}</p>
            {replacement && <p className="meta">A request to move this appointment to {localStamp(replacement.start_at, zone)} is
              waiting for the owner. This appointment stays confirmed until the owner approves it.</p>}
            {canChange && <div className="actions">
              {booking.status === "CONFIRMED" && !replacement && <button type="button" onClick={() => startMove(booking)}>
                Move to another time</button>}
              {booking.status === "CONFIRMED" && replacement ? <span className="meta">To cancel, first withdraw the move request.</span>
                : <button type="button" onClick={() => startCancel(booking)}>
                  {booking.status === "CONFIRMED" ? "Cancel appointment"
                    : isMove ? "Withdraw move request" : "Cancel request"}</button>}
            </div>}
            {confirming?.appointment_id === booking.appointment_id && <div role="group" aria-label="Confirm cancellation" className="notice">
              <p>{booking.status === "CONFIRMED" ? "Cancel this confirmed appointment?" : isMove
                ? "Withdraw this move request? Your original appointment stays confirmed." : "Cancel this request?"}
                {" "}<strong>{localStamp(booking.start_at, zone)}</strong> to {localTime(booking.end_at, zone)} ({zone}).
                {booking.status === "CONFIRMED" ? " The time is released and the owner is told." : ""}</p>
              <button type="button" disabled={changing} onClick={() => void confirmCancel()}>
                {changing ? "Cancelling…" : isMove ? "Yes, withdraw this request" : "Yes, cancel this " + (booking.status === "CONFIRMED" ? "appointment" : "request")}</button>
              <button type="button" disabled={changing} onClick={() => { setConfirming(null); setChangeOutcome(null); }}>
                {isMove ? "No, keep the request" : "No, keep it"}</button>
            </div>}
          </li>;
        })}</ul>}
      {changeOutcome?.kind === "cancelled" && <p role="status" className="notice">
        {changeOutcome.booking.status === "CANCELLED"
          ? <>Cancelled: <strong>{localStamp(changeOutcome.booking.start_at, zone)}</strong> to {localTime(changeOutcome.booking.end_at, zone)}.
            The time was released and the owner was told.</>
          : `This is no longer active: ${bookingState(changeOutcome.booking, nowMs).label}.`}</p>}
      {changeOutcome?.kind === "error" && <p role="alert" className="notice error">{changeOutcome.message}</p>}
    </section>
  </div>;
}
