import { useState } from "react";

import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";
import { PolicyField } from "./PolicyField";
import { applyRegular, dayKey, normalizeRegular, regularDraft, regularErrors, REGULAR_LABELS, WEEKDAYS, type RegularDraft } from "./policyDraft";
import { ConflictNotice, FailureAlert } from "./FailureAlert";
import { usePolicySave } from "./usePolicySave";
import { useSyncedDraft } from "./useSyncedDraft";

export function RegularPolicyForm() {
  const { data } = useOwner();
  const policy = data.policy?.record.policy ?? null;
  const { draft, setDraft, dirty, reset, conflicts } = useSyncedDraft<RegularDraft>(policy ? regularDraft(policy) : null);
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
    edit((current) => ({ ...current, [dayKey(day)]: current[dayKey(day)]
      .map((window, i) => i === index ? { ...window, [field]: value } : window) }));
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
    // Store the draft as the server will, so it matches the saved value afterwards; input typed while
    // the save is in flight stays.
    const sent = normalizeRegular(draft);
    setDraft(() => sent);
    if (await save((saved) => applyRegular(saved, sent))) setShown(false);
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
          {draft[dayKey(day)].length === 0 && <span className="meta"> Closed</span>}
          {draft[dayKey(day)].map((window, index) => {
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
                [dayKey(day)]: current[dayKey(day)].filter((_, i) => i !== index) }))}
                aria-label={`Remove ${name} hours ${index + 1}`}>Remove</button>
            </div>;
          })}
          <button type="button" onClick={() => edit((current) => ({ ...current,
            [dayKey(day)]: [...current[dayKey(day)], { opens: "08:00", closes: "17:00" }] }))}
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
      <ConflictNotice labels={conflicts.map((key) => REGULAR_LABELS[key as keyof typeof REGULAR_LABELS])} />
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
