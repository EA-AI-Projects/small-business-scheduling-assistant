import { useCallback, useEffect, useRef, useState } from "react";

import type { CalendarState } from "@/calendar/useCalendarState";
import { bookingState, calendarItems, hasStalePending, hasVersion, nextHoldExpiry, parseAvailability, parseBookings, pendingReplacement, type ClientBooking } from "@/lib/clientBookings";
import type { OwnerConfig } from "@/lib/config";
import { addDays, datesForView, dayKey, localStamp, localTime, todayKey } from "@/lib/time";
import { PopoverCard } from "@/components/PopoverCard";

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
  DURATION_CHANGED: "The visit length changed after those times were shown, so nothing was sent. Choose from the new times.",
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
    stale: code === "STALE_BOOKING" || code === "BOOKING_NOT_ACTIVE" || code === "BOOKING_NOT_RESCHEDULABLE"
      || code === "DURATION_CHANGED" };
  if (response.status === 404) return { kind: "error", message: "That appointment was not found. Refresh to see your appointments." };
  if (response.status === 503) return { kind: "error", message: timed ? "Online requests are not available right now." : "Online changes are not available right now." };
  return { kind: "error", message: timed ? "Your request was not sent. Please try again."
    : "Nothing was changed. Please try again." };
}

/** A visit being moved. It lives in the signed-in shell so Move on Appointments carries over to the Calendar's Day view. */
export interface MoveState { booking: ClientBooking | null; set: (booking: ClientBooking | null) => void }

/**
 * Client pages, in the business time zone. With `calendar` (the Calendar page) it shows the client's own
 * bookings and open times in the shared Day and Week views; choosing one and confirming sends a
 * request for owner approval, never an appointment until the owner approves. Without it (the Appointments
 * page) it lists the bookings with cancel, withdraw and move. The visit length is the business's saved default
 * duration, chosen by the server.
 */
export function ClientHome({ config, token, zone, onSessionEnded, calendar, horizonDays, move, onOpenCalendar, visitLabel }: {
  config: OwnerConfig; token: string; zone: string | null; onSessionEnded: () => void;
  /** The saved service and business name, shown where the page describes the visit. */
  visitLabel?: string | null;
  /** Given on the Calendar page: its shared view state drives Day to Year views. Omitted on Appointments. */
  calendar?: CalendarState;
  /** Days ahead the business takes bookings, when the server said. */
  horizonDays?: number | null;
  /** Shared move state; the page holds its own when omitted. */
  move?: MoveState;
  /** Appointments: open the Calendar's Day view so a new time can be picked for a move. */
  onOpenCalendar?: () => void;
}) {
  if (!zone) return <section className="card"><p className="notice error" role="alert">
    Online times are not available right now. Please text the business.</p></section>;
  return <ClientCalendar config={config} token={token} zone={zone} onSessionEnded={onSessionEnded}
    calendar={calendar} horizonDays={horizonDays ?? null} move={move} onOpenCalendar={onOpenCalendar}
    visitLabel={visitLabel ?? null} />;
}

function ClientCalendar({ config, token, zone, onSessionEnded, calendar, horizonDays, move, onOpenCalendar, visitLabel }: {
  config: OwnerConfig; token: string; zone: string; onSessionEnded: () => void; visitLabel: string | null;
  calendar: CalendarState | undefined; horizonDays: number | null;
  move: MoveState | undefined; onOpenCalendar: (() => void) | undefined;
}) {
  const date = calendar ? calendar.date : "";
  const setDate = (day: string) => calendar?.goToDate(day);
  // Availability is queried only for visible days within the booking horizon.
  const today = todayKey(zone);
  const maxDate = horizonDays === null ? null : addDays(today, horizonDays);
  const beyondHorizon = maxDate !== null && date > maxDate;
  const availabilityDays = calendar && (calendar.view === "day" || calendar.view === "week")
    ? datesForView(date, calendar.view).filter((day) => day >= today && (maxDate === null || day <= maxDate)) : [];
  const availabilityDaysKey = availabilityDays.join(",");
  const [bookings, setBookings] = useState<ClientBooking[] | null>(null);
  const [bookingsError, setBookingsError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [chosen, setChosen] = useState<string | null>(null);
  const chosenAnchor = useRef<HTMLElement | null>(null);
  // One key per chosen time, reused on a retry so a lost response cannot create a second request.
  const attempt = useRef<{ id: string; key: string } | null>(null);
  // A visit being moved: the confirmed original stays booked until the owner approves the replacement.
  const [ownMoving, setOwnMoving] = useState<ClientBooking | null>(null);
  const heldMoving = move ? move.booking : ownMoving;
  const setMoving = move ? move.set : setOwnMoving;
  const [moveDropped, setMoveDropped] = useState(false);
  // Read by the bookings load, which clears a move whose booking is gone or changed.
  const heldMove = useRef({ held: heldMoving, set: setMoving, dropped: setMoveDropped });
  useEffect(() => { heldMove.current = { held: heldMoving, set: setMoving, dropped: setMoveDropped }; }, [heldMoving, setMoving]);
  // A booking whose cancellation is waiting for the separate, explicit confirm press.
  const [heldConfirming, setConfirming] = useState<ClientBooking | null>(null);
  const cancelAttempt = useRef<{ id: string; key: string } | null>(null);
  const [changing, setChanging] = useState(false);
  const [changeOutcome, setChangeOutcome] = useState<{ kind: "cancelled"; booking: ClientBooking }
    | { kind: "error"; message: string } | null>(null);
  const [sending, setSending] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [availability, setAvailability] = useState<{ key: string; starts: string[] | null; recommended: string[];
    minutes: number | null; error: string | null } | null>(null);
  const availabilityKey = `${availabilityDaysKey}|${refresh}`;

  const fail = useCallback((error: unknown, set: (message: string) => void) => {
    if (error instanceof Unauthorized) onSessionEnded();
    else set(error instanceof Error ? error.message : "Something went wrong.");
  }, [onSessionEnded]);

  useEffect(() => {
    let current = true;
    read(config, token, "/v1/client/bookings").then((data) => {
      if (!current) return;
      const list = parseBookings(data);
      const { held, set, dropped } = heldMove.current;
      if (held && !list.some((item) => item.appointment_id === held.appointment_id && item.version === held.version)) {
        set(null); dropped(true);
      }
      setBookings(list); setBookingsError(null); setNowMs(Date.now());
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
    if (!availabilityDaysKey) return;
    let current = true;
    const key = availabilityKey;
    Promise.all(availabilityDaysKey.split(",").map((day) =>
      read(config, token, `/v1/client/availability?day=${day}`)))
      .then((data) => {
        if (!current) return;
        const parsed = data.map(parseAvailability);
        setAvailability({ key, starts: parsed.flatMap((result) => result.starts),
          recommended: parsed.flatMap((result) => result.recommended),
          minutes: parsed[0]?.durationMinutes ?? null, error: null });
      })
      .catch((error: unknown) => { if (current) fail(error, (message) =>
        setAvailability({ key, starts: null, recommended: [], minutes: null, error: message })); });
    return () => { current = false; };
  }, [config, token, availabilityDaysKey, availabilityKey, fail]);

  // A re-read that changed a booking's version drops any panel still holding the old one.
  const stillCurrent = (held: ClientBooking | null) => held && bookings?.some((item) =>
    item.appointment_id === held.appointment_id && item.version === held.version) ? held : null;
  const moving = stillCurrent(heldMoving);
  // A held move whose booking is not loaded yet must never fall back to a new request.
  const moveLoading = heldMoving !== null && bookings === null;
  const confirming = stillCurrent(heldConfirming);
  const choose = (start: string | null) => { setChosen(start); setOutcome(null); };
  // In calendar mode a new date or view drops any chosen time and outcome, as picking a day always did.
  const calendarView = calendar?.view;
  const lastSeen = useRef({ date, view: calendarView });
  useEffect(() => {
    if (!calendar) return;
    if (lastSeen.current.date === date && lastSeen.current.view === calendarView) return;
    const keepAlternative = lastSeen.current.view === "day" && calendarView === "day"
      && chosen !== null && dayKey(chosen, zone) === date;
    lastSeen.current = { date, view: calendarView };
    if (!keepAlternative) { setChosen(null); setOutcome(null); attempt.current = null; }
  }, [calendar, date, calendarView, chosen, zone]);
  const send = async () => {
    if (!chosen || sending || moveLoading || (heldMoving && !moving)) return;
    const id = `${moving?.appointment_id ?? "new"}|${moving?.version ?? 0}|${chosen}`;
    if (attempt.current?.id !== id) attempt.current = { id, key: crypto.randomUUID() };
    setSending(true);
    // The length the shown times were quoted at; the server refuses it if the owner has changed it since.
    const quote = availability?.minutes ? { quoted_duration_minutes: availability.minutes } : {};
    try {
      const result = moving && hasVersion(moving)
        ? await write(config, token, `/v1/client/bookings/${encodeURIComponent(moving.appointment_id)}/reschedule`,
          { start_at: chosen, expected_version: moving.version, ...quote }, attempt.current.key, true)
        : await write(config, token, "/v1/client/requests", { start_at: chosen, ...quote }, attempt.current.key, true);
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
    setMoveDropped(false); setMoving(booking); setConfirming(null); setChangeOutcome(null); choose(null);
    if (calendar && calendar.view !== "day" && calendar.view !== "week") {
      calendar.setView("day"); calendar.goToday();
    }
    onOpenCalendar?.();
  };

  const settled = availability?.key === availabilityKey ? availability : null;
  const starts = settled?.starts ?? null;
  const startsError = settled?.error ?? null;
  const shown = (starts ?? []).filter((start) => availabilityDays.includes(dayKey(start, zone)));
  const selectStart = (start: string, element: HTMLElement) => {
    chosenAnchor.current = element;
    choose(start);
  };
  const requestFeedback = (
    <>
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
    </>
  );
  if (calendar) {
    const items = bookings ? calendarItems(bookings, nowMs) : [];
    return <div className="stack client-home">
      <section className="card" aria-label="Your calendar">
        {visitLabel && <p className="meta">Booking: {visitLabel}.</p>}
        <p className="meta">Only your own appointments and requests are shown. Times are shown in the business time zone ({zone}).
          A request is not an appointment until the owner approves it.</p>
        <ClientCalendarViews items={items} bookings={bookings ?? []} calendar={calendar} zone={zone} nowMs={nowMs}
          loading={bookings === null && !bookingsError} error={bookingsError}
          maxDate={maxDate} availableStarts={moveLoading ? [] : shown}
          recommendedStarts={settled?.recommended ?? []} selectedStart={chosen}
          onAvailableSelect={selectStart} onEventSelect={() => choose(null)}
          confirming={confirming} changing={changing}
          changeError={changeOutcome?.kind === "error" ? changeOutcome.message : null}
          onCancel={startCancel} onConfirmCancel={() => void confirmCancel()}
          onDismissCancel={() => { setConfirming(null); setChangeOutcome(null); }} onMove={startMove} />
        {beyondHorizon && calendar.view === "day" && <p role="status">We only book up to {horizonDays} days ahead.</p>}
        {availabilityDays.length > 0 && (startsError ? <p className="notice error" role="alert">{startsError}</p>
          : starts === null || moveLoading ? <p role="status">Loading times…</p>
          : shown.length === 0 ? <p role="status">No times are available in this view. Try another day.</p> : null)}
        {settled?.minutes && <p className="meta">Visits are about {settled.minutes} minutes.</p>}
        {requestFeedback}
        {changeOutcome?.kind === "cancelled" && <p role="status" className="notice">
          {changeOutcome.booking.status === "CANCELLED"
            ? <>Cancelled: <strong>{localStamp(changeOutcome.booking.start_at, zone)}</strong> to {localTime(changeOutcome.booking.end_at, zone)}.
              The time was released and the owner was told.</>
            : `This is no longer active: ${bookingState(changeOutcome.booking, nowMs).label}.`}</p>}
        {changeOutcome?.kind === "error" && <p role="alert" className="notice error">{changeOutcome.message}</p>}
      </section>
      {chosen && <PopoverCard title="Request this time" getAnchor={() => chosenAnchor.current?.isConnected
        ? chosenAnchor.current : document.querySelector<HTMLElement>("[data-calendar-scroll]")}
        onClose={() => choose(null)} returnFocus={() => chosenAnchor.current?.isConnected
          ? chosenAnchor.current : document.querySelector<HTMLElement>("[data-calendar-scroll]")}
        refocusKey={chosen}>
        <p>You picked <strong>{localStamp(chosen, zone)}</strong> ({zone}){visitLabel ? ` for ${visitLabel}` : ""}. Sending asks the owner for approval.
          This time is not booked and is not confirmed until the owner approves it.
          {moving ? ` Your appointment on ${localStamp(moving.start_at, zone)} stays confirmed until then.` : ""}</p>
        <button type="button" disabled={sending || moveLoading} onClick={() => void send()}>
          {sending ? "Sending…" : moving ? "Send move request for owner approval" : "Send request for owner approval"}</button>
        {outcome?.kind === "error" && <p role="alert" className="notice error">{outcome.message}</p>}
      </PopoverCard>}
      {moveDropped && !moving && <p className="notice error" role="alert">
        The appointment you were moving changed or is no longer there, so the move was cancelled and nothing was sent.
        Open Appointments to review it.</p>}
      {moving && <p className="notice" role="status">
        You are moving your appointment on <strong>{localStamp(moving.start_at, zone)}</strong>. Pick a time in Day or Week view.
        Your current appointment stays confirmed until the owner approves the new time. You can also{" "}
        <button type="button" onClick={() => { setMoving(null); choose(null); }}>keep my current appointment</button>.</p>}
    </div>;
  }
  return <div className="stack client-home">
    {moving && <p className="notice" role="status">
      You started moving your appointment on <strong>{localStamp(moving.start_at, zone)}</strong>. Choose a new time in
      Calendar, Day view. Nothing changes until you send it and the owner approves.{" "}
      <button type="button" onClick={() => setMoving(null)}>Keep my current appointment</button></p>}
    <section className="card" aria-labelledby="client-bookings">
      <h2 id="client-bookings">Your appointments</h2>
      {visitLabel && <p className="meta">{visitLabel}</p>}
      <button type="button" onClick={() => { setConfirming(null); setMoving(null); setRefresh((count) => count + 1); }}>Refresh</button>
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
