import { useCallback, useEffect, useState } from "react";

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

function problems(draft: Draft, maximumVisit: number | null): string[] {
  const found: string[] = [];
  const clean = normalized(draft);
  const labels: Record<keyof typeof LIMITS, string> = { display_name: "Business name",
    description: "Business description", support_contact: "Support contact",
    service_name: "Service name", service_description: "Service description" };
  for (const key of Object.keys(LIMITS) as (keyof typeof LIMITS)[]) {
    if (!clean[key]) found.push(`Enter a ${labels[key].toLowerCase()}.`);
    else if (clean[key].length > LIMITS[key]) found.push(`${labels[key]} must be ${LIMITS[key]} characters or fewer.`);
  }
  const minutes = Number(clean.default_duration_minutes);
  if (!clean.default_duration_minutes || !Number.isInteger(minutes) || minutes < 1 || minutes > MAX_MINUTES) {
    found.push(`Default duration must be a whole number of minutes from 1 to ${MAX_MINUTES}.`);
  } else if (maximumVisit !== null && minutes > maximumVisit) {
    found.push(`Default duration cannot exceed the current maximum visit of ${maximumVisit} minutes.`);
  }
  return found;
}

export function BusinessProfileForm() {
  const { api, data, notify } = useOwner();
  const [saved, setSaved] = useState<BusinessProfileState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [shown, setShown] = useState(false);
  const [stale, setStale] = useState(false);
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
  const visible = shown ? found : [];

  function edit(key: keyof Draft, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
    setServerError(null);
  }

  async function save() {
    if (!saved) return;
    setShown(true);
    setServerError(null);
    if (found.length) return;
    const clean = normalized(draft);
    const body: BusinessProfileEditBody = { expected_version: saved.version, profile: {
      ...clean, default_duration_minutes: Number(clean.default_duration_minutes) } };
    try {
      const updated = await api.request<BusinessProfileState>("/profile", { method: "PUT", body,
        idempotent: true });
      setSaved(updated);
      setDraft(toDraft(updated.profile));
      setStale(false);
      setShown(false);
      notify("Business details saved");
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
      <label>Business name
        <input value={draft.display_name}
          onChange={(event) => edit("display_name", event.target.value)} />
      </label>
      <label>Business description
        <textarea value={draft.description} rows={3}
          onChange={(event) => edit("description", event.target.value)} />
      </label>
      <label>Support contact
        <input value={draft.support_contact}
          onChange={(event) => edit("support_contact", event.target.value)} />
      </label>
      <label>Service name
        <input value={draft.service_name}
          onChange={(event) => edit("service_name", event.target.value)} />
      </label>
      <label>Service description
        <textarea value={draft.service_description} rows={3}
          onChange={(event) => edit("service_description", event.target.value)} />
      </label>
      <label>Default booking duration (minutes)
        <input type="number" inputMode="numeric" min={1} max={MAX_MINUTES} step={1}
          value={draft.default_duration_minutes}
          onChange={(event) => edit("default_duration_minutes", event.target.value)} />
      </label>
      <p className="hint">Changing the default duration affects future requests only. Existing
        appointments keep the duration they already have.</p>
      {stale && <div className="field-error" role="alert">
        <p>These details were changed elsewhere (now version {saved.version}). Your edits are kept;
          review them and save again to replace the newer values.</p>
        <BusyButton onClick={load}>Discard my edits and show saved values</BusyButton>
      </div>}
      {visible.map((message) => <p key={message} className="field-error" role="alert">{message}</p>)}
      {serverError && <p className="field-error" role="alert">{serverError}</p>}
      {dirty && <p className="meta" role="status">You have unsaved changes.</p>}
      <div><BusyButton className="primary" onClick={save}>Save business details</BusyButton></div>
      <p className="meta">Version {saved.version}</p>
    </div>}
  </section>;
}
