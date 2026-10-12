import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import type { BusinessProfileBody, BusinessProfileEditBody, BusinessProfileState } from "@/api/types";
import { ApiError } from "@/lib/api";
import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";

const LIMITS = { display_name: 100, description: 300, support_contact: 200, service_name: 100,
  service_description: 300 } as const;
const MAX_MINUTES = 1440;

const EMPTY: Record<keyof BusinessProfileBody, string> = { display_name: "", description: "",
  support_contact: "", service_name: "", service_description: "", default_duration_minutes: "" };

type Draft = typeof EMPTY;

function toDraft(profile: BusinessProfileBody | null): Draft {
  if (!profile) return EMPTY;
  return { ...profile, default_duration_minutes: String(profile.default_duration_minutes) };
}

function normalized(draft: Draft): Draft {
  return Object.fromEntries(Object.entries(draft).map(([key, value]) => [key, value.trim()])) as Draft;
}

type Errors = Partial<Record<keyof Draft, string>>;

const LABELS: Record<keyof typeof LIMITS, string> = { display_name: "Business name",
  description: "Business description", support_contact: "Support contact",
  service_name: "Service name", service_description: "Service description" };

/** Length as the server counts it: Unicode code points, not UTF-16 units. */
function length(value: string): number {
  return [...value].length;
}

function problems(draft: Draft, maximumVisit: number | null): Errors {
  const found: Errors = {};
  const clean = normalized(draft);
  for (const key of Object.keys(LIMITS) as (keyof typeof LIMITS)[]) {
    if (!clean[key]) found[key] = `Enter a ${LABELS[key].toLowerCase()}.`;
    else if (length(clean[key]) > LIMITS[key]) found[key] = `${LABELS[key]} must be ${LIMITS[key]} characters or fewer.`;
  }
  const minutes = Number(clean.default_duration_minutes);
  if (!clean.default_duration_minutes || !Number.isInteger(minutes) || minutes < 1 || minutes > MAX_MINUTES) {
    found.default_duration_minutes = `Default duration must be a whole number of minutes from 1 to ${MAX_MINUTES}.`;
  } else if (maximumVisit !== null && minutes > maximumVisit) {
    found.default_duration_minutes = `Default duration cannot exceed the current maximum visit of ${maximumVisit} minutes.`;
  }
  return found;
}

function Field({ name, label, error, shown, extra, children }: {
  name: keyof Draft; label: string; error: string | undefined; shown: boolean; extra?: string;
  children: (props: { id: string; "aria-invalid": boolean; "aria-describedby": string | undefined }) => ReactNode;
}) {
  const id = `business-profile-${name}`;
  const showError = shown && error !== undefined;
  const described = [showError ? `${id}-error` : null, extra ?? null].filter(Boolean).join(" ") || undefined;
  return <div className="form-field">
    <label htmlFor={id}>{label}</label>
    {children({ id, "aria-invalid": showError, "aria-describedby": described })}
    {showError && <p id={`${id}-error`} className="field-error">{error}</p>}
  </div>;
}

export function BusinessProfileForm() {
  const { api, data, notify, refresh } = useOwner();
  const [saved, setSaved] = useState<BusinessProfileState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [shown, setShown] = useState(false);
  const [stale, setStale] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const state = await api.get<BusinessProfileState>("/profile");
      setSaved(state);
      setDraft(toDraft(state.profile));
      setLoadError(null);
      setStale(false);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error));
    }
  }, [api]);
  useEffect(() => {
    let live = true;
    api.get<BusinessProfileState>("/profile").then((state) => {
      if (!live) return;
      setSaved(state);
      setDraft(toDraft(state.profile));
    }, (error: unknown) => {
      if (live) setLoadError(error instanceof Error ? error.message : String(error));
    });
    return () => { live = false; };
  }, [api]);

  const savedDraft = toDraft(saved?.profile ?? null);
  const dirty = saved !== null && JSON.stringify(normalized(draft)) !== JSON.stringify(savedDraft);
  const found = problems(draft, data.policy?.record.policy.maximum_visit_minutes ?? null);
  const count = Object.keys(found).length;
  // One key per exact request, so a retry after a lost response replays instead of conflicting.
  const attempt = useRef<{ signature: string; key: string } | null>(null);

  function edit(key: keyof Draft, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
    setServerError(null);
  }

  async function save() {
    if (!saved) return;
    setShown(true);
    setServerError(null);
    setSummary(null);
    if (count) {
      setSummary(`Fix ${count} ${count === 1 ? "field" : "fields"} before saving.`);
      return;
    }
    const clean = normalized(draft);
    const body: BusinessProfileEditBody = { expected_version: saved.version, profile: {
      ...clean, default_duration_minutes: Number(clean.default_duration_minutes) } };
    const signature = JSON.stringify(body);
    if (attempt.current?.signature !== signature) {
      attempt.current = { signature, key: crypto.randomUUID() };
    }
    try {
      const updated = await api.request<BusinessProfileState>("/profile", { method: "PUT", body,
        idempotencyKey: attempt.current.key });
      attempt.current = null;
      setSaved(updated);
      // Keep anything typed while the save was in flight.
      setDraft((current) => JSON.stringify(normalized(current)) === JSON.stringify(normalized(toDraft(body.profile)))
        ? toDraft(updated.profile) : current);
      setStale(false);
      setShown(false);
      notify("Business details saved");
      // Owner texts that name the business and service (invitation wording) read it from there.
      void refresh(true).catch(() => undefined);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.code === "STALE_VERSION") {
        const current = error.current as BusinessProfileState | undefined;
        if (current) setSaved(current);
        setStale(true);
        notify("Nothing was saved: the business details changed since you opened them", true);
      } else {
        setServerError(error instanceof Error ? error.message : String(error));
        notify("Business details were not saved", true);
      }
    }
  }

  return <section className="card business-profile-card">
    <h3>Business details</h3>
    <p className="hint">The business description, support contact, and the one service you offer.
      Saved values are the source of business information for the owner app and booking flows.</p>
    {loadError && <p className="field-error" role="alert">Business details could not load: {loadError}</p>}
    {saved === null && !loadError && <p className="empty">Loading business details…</p>}
    {saved === null && loadError && <div><BusyButton onClick={load}>Retry</BusyButton></div>}
    {saved && <div className="form-stack">
      {saved.profile === null && <p className="empty">Not set up yet. Enter your details and save.</p>}
      <Field name="display_name" label="Business name" error={found.display_name} shown={shown}>
        {(props) => <input {...props} value={draft.display_name}
          onChange={(event) => edit("display_name", event.target.value)} />}
      </Field>
      <Field name="description" label="Business description" error={found.description} shown={shown}>
        {(props) => <textarea {...props} rows={3} value={draft.description}
          onChange={(event) => edit("description", event.target.value)} />}
      </Field>
      <Field name="support_contact" label="Support contact" error={found.support_contact} shown={shown}>
        {(props) => <input {...props} value={draft.support_contact}
          onChange={(event) => edit("support_contact", event.target.value)} />}
      </Field>
      <Field name="service_name" label="Service name" error={found.service_name} shown={shown}>
        {(props) => <input {...props} value={draft.service_name}
          onChange={(event) => edit("service_name", event.target.value)} />}
      </Field>
      <Field name="service_description" label="Service description" error={found.service_description} shown={shown}>
        {(props) => <textarea {...props} rows={3} value={draft.service_description}
          onChange={(event) => edit("service_description", event.target.value)} />}
      </Field>
      <Field name="default_duration_minutes" label="Default booking duration (minutes)"
        error={found.default_duration_minutes} shown={shown} extra="business-profile-duration-hint">
        {(props) => <input {...props} type="number" inputMode="numeric" min={1} max={MAX_MINUTES} step={1}
          value={draft.default_duration_minutes}
          onChange={(event) => edit("default_duration_minutes", event.target.value)} />}
      </Field>
      <p id="business-profile-duration-hint" className="hint">Changing the default duration affects
        future requests only. Existing appointments keep the duration they already have.</p>
      {stale && <div className="field-error" role="alert">
        <p>These details were changed elsewhere (now version {saved.version}). Your edits are kept;
          review them and save again to replace the newer values.</p>
        <BusyButton onClick={load}>Discard my edits and show saved values</BusyButton>
      </div>}
      {summary && <p className="field-error" role="alert">{summary}</p>}
      {serverError && <p className="field-error" role="alert">{serverError}</p>}
      {dirty && <p className="meta" role="status">You have unsaved changes.</p>}
      <div><BusyButton className="primary" onClick={save}>Save business details</BusyButton></div>
      <p className="meta">Version {saved.version}</p>
    </div>}
  </section>;
}
