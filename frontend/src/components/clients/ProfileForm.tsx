import type { FormEvent } from "react";

import type { ClientProfile, ClientProfileBody, HomeSize } from "@/api/types";
import { path } from "@/lib/api";
import { useOwner } from "@/owner/OwnerContext";

function text(form: FormData, name: string): string {
  return String(form.get(name) ?? "").trim();
}

/**
 * Create or update one client profile. Uncontrolled: the parent remounts it (by key)
 * whenever the selection changes or a refresh delivers the selected client's current data.
 */
export function ProfileForm({ client, onCreated, onPhoneDraft }: {
  client: ClientProfile | undefined;
  /** Called with the id after a new client is saved, to start onboarding. */
  onCreated?: (clientId: string) => void;
  /** Reports the phone as typed, so the consent step can wait for a saved number. */
  onPhoneDraft?: (phone: string) => void;
}) {
  const { data, change, selectClient } = useOwner();

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const id = text(form, "client_id");
    const current = data.clients.find((item) => item.client_id === id);
    const body: ClientProfileBody = {
      expected_version: current?.version ?? 0,
      name: text(form, "name"),
      phone_e164: text(form, "phone_e164"),
      service_address: text(form, "service_address"),
      home_size: String(form.get("home_size")) as HomeSize,
      default_duration_minutes: Number(form.get("default_duration_minutes")),
      active: form.get("active") === "on",
    };
    if (await change(path`/clients/${id}`, "PUT", body, "Client profile saved", false)) {
      selectClient(id);
      if (!current) onCreated?.(id);
    }
  }

  return (
    <section className="card">
      <h3>Profile</h3>
      {client && (
        <p className="hint" data-testid="phone-status">
          {client.phone_verified_at
            ? "Phone verified for texting (in-person consent recorded)."
            : "Phone not verified: this client cannot text until in-person consent is recorded."}
        </p>
      )}
      <form className="form-stack" aria-label="Client profile" onSubmit={submit}>
        <label>Client ID <input name="client_id" required readOnly={client !== undefined}
          defaultValue={client?.client_id ?? ""} /></label>
        <label>Name <input name="name" required maxLength={200} defaultValue={client?.name ?? ""} /></label>
        <label>Phone (E.164) <input name="phone_e164" type="tel" placeholder="+14155550101" required
          defaultValue={client?.phone_e164 ?? ""}
          onChange={(event) => onPhoneDraft?.(event.target.value)} /></label>
        <label>Service address <input name="service_address" required maxLength={500}
          defaultValue={client?.service_address ?? ""} /></label>
        <label>Home size <select name="home_size" defaultValue={client?.home_size ?? "small"}>
          <option value="small">Small</option>
          <option value="medium">Medium</option>
          <option value="large">Large</option>
        </select></label>
        <label>Default minutes <input name="default_duration_minutes" type="number" min={1} required
          defaultValue={client?.default_duration_minutes ?? ""} /></label>
        <label className="inline"><input name="active" type="checkbox"
          defaultChecked={client?.active ?? true} /> Active</label>
        <button className="primary" type="submit">Save profile</button>
      </form>
    </section>
  );
}
