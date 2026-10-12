import { useState } from "react";

import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";
import { PolicyField } from "./PolicyField";
import { ADVANCED_FIELDS, ADVANCED_LABELS, advancedDraft, advancedErrors, applyAdvanced, normalizeAdvanced, type AdvancedDraft,
  type AdvancedField } from "./policyDraft";
import { ConflictNotice, FailureAlert } from "./FailureAlert";
import { usePolicySave } from "./usePolicySave";
import { useSyncedDraft } from "./useSyncedDraft";

const HINTS: Record<AdvancedField, string> = {
  slot_increment_minutes: "Bookable start times fall on this many minutes within open hours.",
  maximum_visit_minutes: "The longest visit that can be booked. The default booking duration in Business details cannot exceed it.",
  maximum_buffer_minutes: "The most time any visit may reserve after itself. It must cover the gap between visits.",
  minimum_visit_gap_minutes: "Minimum time between two visits, for travel.",
  opening_buffer_minutes: "Time after opening before the first visit may start.",
  closing_buffer_minutes: "Time before closing that the last visit must end.",
  hold_minutes: "How long a pending request holds its time before it expires.",
};

function timezoneNames(): string[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
  try { return supported ? supported("timeZone") : []; } catch { return []; }
}

export function AdvancedPolicyForm() {
  const { data } = useOwner();
  const policy = data.policy?.record.policy ?? null;
  const { draft, setDraft, dirty, reset, conflicts, rebase } = useSyncedDraft<AdvancedDraft>(policy ? advancedDraft(policy) : null);
  const { save, failure, clearFailure } = usePolicySave("Advanced scheduling settings saved");
  const [shown, setShown] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  if (!policy || !draft) return null;

  const found = advancedErrors(draft);
  const count = Object.keys(found).length;
  const zoneChanged = draft.timezone.trim() !== policy.timezone;

  function edit(key: keyof AdvancedDraft, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
    clearFailure();
  }

  async function submit() {
    setShown(true);
    setSummary(null);
    clearFailure();
    if (count) {
      setSummary(`Fix ${count} ${count === 1 ? "field" : "fields"} before saving.`);
      return;
    }
    if (!draft) return;
    // Store the draft as the server will, so it matches the saved value afterwards.
    const sent = normalizeAdvanced(draft);
    setDraft(() => sent);
    if (await save((saved) => applyAdvanced(saved, sent), () => rebase(sent))) setShown(false);
  }

  return <section className="card advanced-policy-card">
    <details>
      <summary><strong>Advanced scheduling</strong></summary>
      <p className="hint">Technical limits for how visits are placed. Most owners never change these. An edit that
        would invalidate an active booking or pending request is rejected.</p>
      <div className="form-stack">
        {ADVANCED_FIELDS.map((key) => <PolicyField key={key} id={`advanced-policy-${key}`}
          label={`${ADVANCED_LABELS[key].label} (minutes)`} error={found[key]} shown={shown} hint={HINTS[key]}>
          {(props) => <input {...props} type="number" inputMode="numeric" min={ADVANCED_LABELS[key].minimum}
            step={1} value={draft[key]} onChange={(event) => edit(key, event.target.value)} />}
        </PolicyField>)}
        <PolicyField id="advanced-policy-timezone" label="Business timezone" error={found.timezone} shown={shown}>
          {(props) => <>
            <input {...props} list="advanced-policy-timezones" value={draft.timezone}
              aria-describedby={[props["aria-describedby"], "advanced-policy-timezone-warning"].filter(Boolean).join(" ")}
              onChange={(event) => edit("timezone", event.target.value)} />
            <datalist id="advanced-policy-timezones">
              {timezoneNames().map((name) => <option key={name} value={name} />)}
            </datalist>
          </>}
        </PolicyField>
        <p id="advanced-policy-timezone-warning" className={zoneChanged ? "field-error" : "hint"}>
          Warning: the timezone sets how every date and time in the app is displayed and how weekly hours and
          date exceptions are read. Existing visits keep their exact moment in time, but they will show at different
          clock times, and the new hours may no longer fit them. A change that conflicts with an active booking or
          pending request is rejected.
        </p>
        {summary && <p className="field-error" role="alert">{summary}</p>}
        <ConflictNotice labels={conflicts.map((key) => key === "timezone" ? "Business timezone"
          : ADVANCED_LABELS[key as AdvancedField].label)} />
        <FailureAlert failure={failure} />
        {dirty && <p className="meta" role="status">You have unsaved changes.</p>}
        <div className="row-actions">
          <BusyButton className="primary" onClick={submit}>Save advanced settings</BusyButton>
          {dirty && <button type="button" onClick={() => { reset(); setShown(false); setSummary(null); clearFailure(); }}>
            Discard my edits</button>}
        </div>
      </div>
    </details>
  </section>;
}
