import type { PolicyState } from "@/api/types";
import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";
import { shortTime } from "./policyEdit";

export function PolicySummary({ policy }: { policy: PolicyState | null }) {
  return (
    <section className="card">
      <h3>Current policy</h3>
      <div>{policy ? <Summary state={policy} /> : <SeedPolicy />}</div>
    </section>
  );
}

function Summary({ state }: { state: PolicyState }) {
  const policy = state.record.policy;
  const exceptions = Object.entries(policy.date_exceptions ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return (
    <>
      <p>{`Timezone: ${policy.timezone}`}</p>
      <p>{`Maximum visit: ${policy.maximum_visit_minutes} minutes · Gap: ${
        policy.minimum_visit_gap_minutes} minutes`}</p>
      <p className="meta">{`Version ${state.record.version} · calendar revision ${state.calendar_revision}`}</p>
      {exceptions.length === 0 && <p className="empty">No date exceptions</p>}
      {exceptions.map(([date, windows]) => (
        <p key={date} className="meta">
          {`${date}: ${windows.length
            ? windows.map((item) => `${shortTime(item.opens)}–${shortTime(item.closes)}`).join(", ")
            : "Closed"}`}
        </p>
      ))}
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
