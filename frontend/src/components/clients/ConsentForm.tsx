import { useState, type FormEvent } from "react";

import type { ClientProfile, InPersonConsentBody } from "@/api/types";
import { path } from "@/lib/api";
import { useOwner } from "@/owner/OwnerContext";

const CONSENT_PAGE = "https://ea-ai-projects.github.io/small-business-scheduling-assistant/sms-consent/";

/**
 * Record that a client clearly said yes in person to receiving texts. No text is sent.
 * Remounted by key when the selection or the client's data changes.
 */
export function ConsentForm({ client }: { client: ClientProfile | undefined }) {
  const { change, notify } = useOwner();
  const [clearYes, setClearYes] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!client) { notify("Select a client first", true); return; }
    if (!clearYes) { notify("Record consent only after they clearly said yes", true); return; }
    const form = new FormData(event.currentTarget);
    const body: InPersonConsentBody = {
      phone_e164: client.phone_e164,
      participant_name: String(form.get("participant_name") ?? "").trim(),
      script_version: String(form.get("script_version") ?? "").trim(),
      clear_yes: true,
    };
    if (await change(path`/clients/${client.client_id}/sms-consent`, "POST", body,
      "Text consent recorded", false)) setClearYes(false);
  }

  return (
    <section className="card">
      <h3>Record in-person text consent</h3>
      <p className="hint">
        Read the exact script on the <a href={CONSENT_PAGE} target="_blank" rel="noreferrer">
        public consent page</a> aloud first. Recording consent sends no text. The full private
        consent record is kept by the owner outside this app.
      </p>
      <form className="form-stack" aria-label="Record in-person text consent" onSubmit={submit}>
        <label>Participant name <input name="participant_name" required maxLength={200}
          defaultValue={client?.name ?? ""} /></label>
        <label>Consent script version <input name="script_version" required maxLength={80}
          placeholder="Version of the script you read" /></label>
        <label className="inline"><input name="clear_yes" type="checkbox" required
          checked={clearYes} onChange={(event) => setClearYes(event.target.checked)} /> They clearly said yes</label>
        <button className="primary" type="submit" disabled={!client}>Record consent</button>
      </form>
    </section>
  );
}
