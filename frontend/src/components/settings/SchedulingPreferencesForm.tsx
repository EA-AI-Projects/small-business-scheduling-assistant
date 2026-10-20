import { useEffect, useRef, useState } from "react";

import type { PreferenceActivateBody, PreferenceReview, PreferencesState } from "@/api/types";
import { ApiError } from "@/lib/api";
import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";
import { clientLabel, describeRule } from "./preferencesText";

const MAX_CHARACTERS = 1000;
const KIND_LABELS: Record<string, string> = { unclear: "Unclear", contradictory: "Contradictory",
  unsupported: "Not supported", ambiguous_client: "Choose a client", unknown_client: "Client not found",
  constraint_override: "Booking rule (cannot be changed here)" };

function length(value: string): number {
  return [...value].length;
}

/**
 * Text entry, Review changes, corrections, Activate. The saved (active) configuration is shown
 * separately and changes only when Activate or Clear succeeds; a review binds to the exact text it
 * interpreted, so editing the text hides Activate until it is reviewed again.
 */
export function SchedulingPreferencesForm({ state, loadError = false, onRetry, onState, onDirtyChange }: {
  state: PreferencesState | null; onState: (state: PreferencesState) => void;
  loadError?: boolean; onRetry?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const { api, data, notify } = useOwner();
  const [draft, setDraft] = useState(state?.active?.text ?? "");
  const [reviewed, setReviewed] = useState<{ text: string; picks: string; review: PreferenceReview } | null>(null);
  // The owner's profile choice for each ambiguous written name; a change needs a new review.
  const [selections, setSelections] = useState<Record<string, string>>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const result = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const attempt = useRef<{ signature: string; key: string } | null>(null);
  const focusResult = useRef(false);
  // The text shown when the editor opened or the last save settled; later server changes are kept apart.
  const activeText = state?.active?.text ?? "";
  const text = draft.trim();
  const dirty = state !== null && text !== activeText;
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (focusResult.current && result.current) { result.current.focus(); focusResult.current = false; }
  }, [reviewed, serverError]);

  const timezone = data.policy?.record.policy.timezone ?? null;
  const active = state?.active ?? null;
  const picks = JSON.stringify(Object.entries(selections).sort());
  const sameText = reviewed !== null && reviewed.text === text;
  const picksCurrent = sameText && reviewed.picks === picks;
  // A ready review stands only for the exact text and picks; a blocked one keeps its pickers while picks change.
  const current = reviewed !== null && picksCurrent ? reviewed.review : null;
  const blocked = sameText && !reviewed.review.ready ? reviewed.review : null;

  function edit(value: string) {
    setDraft(value);
    setServerError(null);
    setFieldError(null);
  }

  async function review() {
    setServerError(null);
    if (!text) {
      setFieldError("Enter the weekdays or start times you prefer.");
      field.current?.focus();
      return;
    }
    if (length(text) > MAX_CHARACTERS) {
      setFieldError(`Preferences must be ${MAX_CHARACTERS} characters or fewer.`);
      field.current?.focus();
      return;
    }
    setFieldError(null);
    try {
      const chosen = Object.entries(selections).map(([name, client_id]) => ({ name, client_id }));
      const body = await api.request<PreferenceReview>("/scheduling-preferences/review",
        { method: "POST", body: chosen.length ? { text, client_selections: chosen } : { text } });
      focusResult.current = true;
      setReviewed({ text, picks, review: body });
    } catch (error) {
      focusResult.current = true;
      setReviewed(null);
      if (error instanceof ApiError && error.code === "INVALID_CLIENT_SELECTION") setSelections({});
      setServerError(`Nothing was reviewed or changed: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  async function activate() {
    if (!state || !current?.ready || !current.review_token) return;
    setServerError(null);
    // Only the token of the server's stored review: the server applies that review, not rules sent here.
    const body: PreferenceActivateBody = { review_token: current.review_token, expected_version: state.version };
    const signature = JSON.stringify(body);
    if (attempt.current?.signature !== signature) attempt.current = { signature, key: crypto.randomUUID() };
    try {
      const updated = await api.request<PreferencesState>("/scheduling-preferences", { method: "PUT", body,
        idempotencyKey: attempt.current.key });
      attempt.current = null;
      onState(updated);
      setReviewed(null);
      setSelections({});
      setStale(false);
      notify("Scheduling preferences activated");
    } catch (error) {
      focusResult.current = true;
      if (error instanceof ApiError && error.status === 409 && error.code === "STALE_VERSION") {
        const latest = error.current as PreferencesState | undefined;
        if (latest) onState(latest);
        setStale(true);
        notify("Nothing was activated: the preferences changed since you opened them", true);
      } else if (error instanceof ApiError && error.code === "REVIEW_MISMATCH") {
        setReviewed(null);
        setServerError(`Nothing was activated: ${error.message}`);
      } else {
        setServerError(`Nothing was activated: ${error instanceof Error ? error.message : String(error)}`);
        notify("Scheduling preferences were not activated", true);
      }
    }
  }

  async function clear() {
    if (!state) return;
    setServerError(null);
    const body = { expected_version: state.version };
    const signature = JSON.stringify(["clear", body]);
    if (attempt.current?.signature !== signature) attempt.current = { signature, key: crypto.randomUUID() };
    try {
      const updated = await api.request<PreferencesState>("/scheduling-preferences", { method: "DELETE", body,
        idempotencyKey: attempt.current.key });
      attempt.current = null;
      onState(updated);
      setDraft("");
      setSelections({});
      setReviewed(null);
      setStale(false);
      notify("Scheduling preferences cleared");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.code === "STALE_VERSION") {
        const latest = error.current as PreferencesState | undefined;
        if (latest) onState(latest);
        setStale(true);
      } else {
        setServerError(`Nothing was cleared: ${error instanceof Error ? error.message : String(error)}`);
      }
      notify("Scheduling preferences were not cleared", true);
    }
  }

  if (!timezone) return <p className="empty">Load the pilot policy under Availability first.</p>;
  return <section className="card scheduling-preferences-card">
    <h3>Scheduling preferences</h3>
    <p className="hint">Tell the assistant which weekdays and visit start times you prefer for the whole
      business or for a particular client. Clients still see every open time; preferred starts are marked Recommended. Hours,
      holidays, buffers, and your approval are never changed by preferences.</p>
    {state === null && loadError && <div className="field-error" role="alert">
      <p>Scheduling preferences could not load. Nothing was changed.</p>
      <BusyButton onClick={async () => { onRetry?.(); }}>Retry</BusyButton>
    </div>}
    {state === null && !loadError && <p className="empty" role="status">Loading scheduling preferences…</p>}
    {state && <div className="form-stack">
      <div className="active-preferences" role="group" aria-label="Active preferences">
        <h4>Active preferences</h4>
        {active ? <>
          <p>{active.text}</p>
          <ul>{active.rules.map((rule) => <li key={JSON.stringify(rule)}>{describeRule(rule, active.clients)}</li>)}</ul>
          <p className="meta">Times are in {active.timezone}.</p>
          {active.timezone !== timezone && <p className="field-error" role="alert">The business timezone is
            now {timezone}, so these preferences are paused. Review and activate them again.</p>}
          <BusyButton onClick={clear}>Clear preferences</BusyButton>
        </> : <p className="empty">None. Clients see ordinary open times without recommendations.</p>}
      </div>
      <div className="form-field">
        <label htmlFor="scheduling-preferences-text">Your preferences (draft)</label>
        <textarea id="scheduling-preferences-text" ref={field} rows={5} value={draft}
          aria-invalid={fieldError !== null} aria-describedby={[fieldError ? "scheduling-preferences-error" : null,
            "scheduling-preferences-hint"].filter(Boolean).join(" ")}
          onChange={(event) => edit(event.target.value)} />
        <p id="scheduling-preferences-hint" className="hint">For example: Prefer mornings generally, and Thursday
          afternoons, and Thursday mornings for Client A. A client&apos;s preference replaces the general one on the
          weekdays it names (default, pending the owner&apos;s confirmation). Times are in {timezone}.</p>
        {fieldError && <p id="scheduling-preferences-error" className="field-error">{fieldError}</p>}
      </div>
      <div><BusyButton onClick={review}>Review changes</BusyButton></div>
      <div ref={result} tabIndex={-1} className="preference-review" aria-label="Review result">
        {reviewed && !sameText && <p className="meta" role="status">The text changed since it was reviewed.
          Review changes again before activating.</p>}
        {reviewed && sameText && !picksCurrent && reviewed.review.ready && <p className="meta" role="status">Your
          selection changed since it was reviewed. Review changes again before activating.</p>}
        {blocked && <div className="field-error" role="alert">
          <p>This cannot be activated yet. Correct the text and review again; nothing was changed.</p>
          <ul>{blocked.problems.map((problem, index) => <li key={index}>
            <strong>{KIND_LABELS[problem.kind] ?? "Needs correction"}:</strong> {problem.message}</li>)}</ul>
          {blocked.ambiguities.map((ambiguity, group) => <fieldset key={ambiguity.name} className="client-picker">
            <legend>Which client is “{ambiguity.name}”?</legend>
            {ambiguity.candidates.map((candidate) => {
              const id = `pick-${group}-${candidate.client_id}`;
              return <div key={candidate.client_id} className="radio-row">
                <input type="radio" id={id} name={`pick-${group}`} value={candidate.client_id}
                  checked={selections[ambiguity.name] === candidate.client_id}
                  onChange={() => setSelections((existing) => ({ ...existing, [ambiguity.name]: candidate.client_id }))} />
                <label htmlFor={id}>{candidate.name}, phone ending {candidate.phone_last4}, {candidate.service_address}</label>
              </div>;
            })}
          </fieldset>)}
          {blocked.ambiguities.length > 0 && !picksCurrent && <p className="meta" role="status">Review again to
            apply your selection.</p>}
          {blocked.ambiguities.length > 0 && picksCurrent && <p className="meta">After choosing, press Review
            changes again.</p>}
        </div>}
        {current?.ready && <div role="status">
          <h4>What the assistant understood</h4>
          {current.clients.length > 0 && <p>Applies to: {current.clients.map((client) => clientLabel(client)).join(", ")}.
            Each preference stays with that profile if the client is renamed.</p>}
          <ul>{current.rules.map((rule) => <li key={JSON.stringify(rule)}>{describeRule(rule, current.clients)}</li>)}</ul>
          <p className="meta">Times are in {current.timezone}. Nothing is active until you activate it.</p>
          <BusyButton className="primary" onClick={activate}>Activate preferences</BusyButton>
        </div>}
        {serverError && <p className="field-error" role="alert">{serverError}</p>}
      </div>
      {stale && <p className="field-error" role="alert">These preferences were changed elsewhere (now version{" "}
        {state.version}). Your draft is kept; review it and activate again to replace the newer ones.</p>}
      {dirty && <p className="meta" role="status">You have an unactivated draft.</p>}
      <p className="meta">Version {state.version}</p>
    </div>}
  </section>;
}
