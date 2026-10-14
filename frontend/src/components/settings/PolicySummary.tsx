import type { PolicyState } from "@/api/types";
import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";
import { WEEKDAYS } from "./policyDraft";
import { shortTime } from "./policyEdit";

export function PolicySummary({ policy, mode = "full" }: { policy: PolicyState | null;
  mode?: "full" | "availability" }) {
  return (
    <section className="card">
      <h3>{mode === "availability" ? "Saved availability" : "Current policy"}</h3>
      <div>{policy ? <Summary state={policy} mode={mode} /> : <SeedPolicy />}</div>
    </section>
  );
}

function hours(windows: { opens: string; closes: string }[]): string {
  return windows.length ? windows.map((item) => `${shortTime(item.opens)}–${shortTime(item.closes)}`).join(", ") : "Closed";
}

function Summary({ state, mode }: { state: PolicyState; mode: "full" | "availability" }) {
  const policy = state.record.policy;
  const exceptions = Object.entries(policy.date_exceptions ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return (
    <>
      <p>{`Timezone: ${policy.timezone}`}</p>
      <h4>Weekly hours</h4>
      <ul className="plain-list">
        {WEEKDAYS.map((name, day) => <li key={name}>{`${name}: ${hours(policy.weekly_windows[String(day)] ?? [])}`}</li>)}
      </ul>
      <p>{policy.holiday_calendar === "US_FEDERAL"
        ? "Holidays: closed on observed US federal holidays"
        : "Holidays: open (normal weekly hours apply)"}</p>
      <p>{`Booking horizon: ${policy.booking_horizon_days} ${policy.booking_horizon_days === 1 ? "day" : "days"} ahead`}</p>
      <h4>Date exceptions</h4>
      {exceptions.length === 0 && <p className="empty">No date exceptions</p>}
      {exceptions.map(([date, windows]) => <p key={date} className="meta">{`${date}: ${hours(windows)}`}</p>)}
      {mode === "full" && <>
        <h4>Advanced scheduling</h4>
        <p>{`Start-time increment: ${policy.slot_increment_minutes} minutes · Maximum visit: ${
          policy.maximum_visit_minutes} minutes · Gap between visits: ${policy.minimum_visit_gap_minutes} minutes`}</p>
        <p>{`Opening buffer: ${policy.opening_buffer_minutes} minutes · Closing buffer: ${
          policy.closing_buffer_minutes} minutes · Maximum buffer: ${policy.maximum_buffer_minutes} minutes · Hold: ${
          policy.hold_minutes} minutes`}</p>
      </>}
      <p className="meta">{`Version ${state.record.version} · calendar revision ${state.calendar_revision}`}</p>
    </>
  );
}

function SeedPolicy() {
  const { change } = useOwner();

  return (
    <>
      <p className="empty">Policy is not configured</p>
      <p className="hint">
        Scheduling writes stay blocked until a policy is saved. Loading the pilot policy saves the
        documented default hours and limits; you can adjust date exceptions afterwards.
      </p>
      <BusyButton className="primary" onClick={() =>
        change("/policy/seed", "POST", undefined, "Pilot policy loaded")}>Load pilot policy</BusyButton>
    </>
  );
}
