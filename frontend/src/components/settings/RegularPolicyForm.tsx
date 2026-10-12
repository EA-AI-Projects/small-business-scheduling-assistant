import { useState } from "react";

import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";
import { PolicyField } from "./PolicyField";
import { applyRegular, regularDraft, regularErrors, WEEKDAYS, type RegularDraft } from "./policyDraft";
import { FailureAlert } from "./FailureAlert";
import { usePolicySave } from "./usePolicySave";
import { useSyncedDraft } from "./useSyncedDraft";

export function RegularPolicyForm() {
  const { data } = useOwner();
  const policy = data.policy?.record.policy ?? null;
  const { draft, setDraft, dirty, reset } = useSyncedDraft<RegularDraft>(policy ? regularDraft(policy) : null);
  const { save, failure, clearFailure } = usePolicySave("Regular scheduling settings saved");
  const [shown, setShown] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  if (!policy || !draft) return null;

  const found = regularErrors(draft);
  const count = Object.keys(found).length;

  function edit(update: (current: RegularDraft) => RegularDraft) {
    setDraft(update);
    clearFailure();
  }
  function editWindow(day: number, index: number, field: "opens" | "closes", value: string) {
    edit((current) => ({ ...current, weekly: current.weekly.map((windows, d) => d !== day ? windows
      : windows.map((window, i) => i === index ? { ...window, [field]: value } : window)) }));
  }

  async function submit() {
    setShown(true);
    setSummary(null);
    clearFailure();
    if (count) {
      setSummary(`Fix ${count} ${count === 1 ? "field" : "fields"} before saving.`);
      return;
    }
    if (!draft || !policy) return;
    // Input typed while the save is in flight stays: the draft only follows the saved policy when unedited.
    if (await save((saved) => applyRegular(saved, draft))) setShown(false);
  }

  return <section className="card regular-policy-card">
    <h3>Regular hours, holidays, and booking window</h3>
    <p className="hint">Weekly hours apply in the business timezone ({policy.timezone}). A day with no hours is
      closed. Changes take effect for new bookings; an edit that would invalidate an active booking or pending
      request is rejected.</p>
    <div className="form-stack">
      <fieldset className="weekly-hours">
        <legend>Weekly hours</legend>
        {WEEKDAYS.map((name, day) => <div key={name} className="weekday-row" role="group" aria-label={name}>
          <strong>{name}</strong>
          {draft.weekly[day]!.length === 0 && <span className="meta"> Closed</span>}
          {draft.weekly[day]!.map((window, index) => {
            const id = `regular-policy-weekly-${day}-${index}`;
            const error = found[`weekly-${day}-${index}`];
            const showError = shown && error !== undefined;
            return <div key={index} className="two-col">
              <label>{`${name} opens`}
                <input type="time" value={window.opens} aria-invalid={showError}
                  aria-describedby={showError ? `${id}-error` : undefined}
                  onChange={(event) => editWindow(day, index, "opens", event.target.value)} /></label>
              <label>{`${name} closes`}
                <input type="time" value={window.closes} aria-invalid={showError}
                  aria-describedby={showError ? `${id}-error` : undefined}
                  onChange={(event) => editWindow(day, index, "closes", event.target.value)} /></label>
              {showError && <p id={`${id}-error`} className="field-error">{error}</p>}
              <button type="button" onClick={() => edit((current) => ({ ...current,
                weekly: current.weekly.map((windows, d) => d === day ? windows.filter((_, i) => i !== index) : windows) }))}
                aria-label={`Remove ${name} hours ${index + 1}`}>Remove</button>
            </div>;
          })}
          <button type="button" onClick={() => edit((current) => ({ ...current,
            weekly: current.weekly.map((windows, d) => d === day
              ? [...windows, { opens: "08:00", closes: "17:00" }] : windows) }))}
            aria-label={`Add ${name} hours`}>Add hours</button>
        </div>)}
      </fieldset>
      <PolicyField id="regular-policy-holidays" label="Holidays" error={undefined} shown={shown}
        hint="Observed US federal holidays follow the Office of Personnel Management schedule. A date exception overrides this for one date.">
        {(props) => <select {...props} value={draft.holidays}
          onChange={(event) => edit((current) => ({ ...current,
            holidays: event.target.value === "US_FEDERAL" ? "US_FEDERAL" : "none" }))}>
          <option value="US_FEDERAL">Closed on observed US federal holidays</option>
          <option value="none">Open on holidays (normal weekly hours)</option>
        </select>}
      </PolicyField>
      <PolicyField id="regular-policy-horizon" label="Booking horizon (days ahead)" error={found.horizon} shown={shown}
        hint="How far ahead clients can see and request times.">
        {(props) => <input {...props} type="number" inputMode="numeric" min={1} step={1} value={draft.horizon}
          onChange={(event) => edit((current) => ({ ...current, horizon: event.target.value }))} />}
      </PolicyField>
      {summary && <p className="field-error" role="alert">{summary}</p>}
      <FailureAlert failure={failure} />
      {dirty && <p className="meta" role="status">You have unsaved changes.</p>}
      <div className="row-actions">
        <BusyButton className="primary" onClick={submit}>Save regular settings</BusyButton>
        {dirty && <button type="button" onClick={() => { reset(); setShown(false); setSummary(null); clearFailure(); }}>
          Discard my edits</button>}
      </div>
    </div>
  </section>;
}
