import { useState, type FormEvent } from "react";

import type { ClientProfile, InPersonConsentBody } from "@/api/types";
import { path } from "@/lib/api";
import { useOwner } from "@/owner/OwnerContext";

/**
 * The consent script the owner reads aloud. Keep in step with the "Script version" line in
 * docs/sms-consent/index.html (checked by ConsentScript.test.ts).
 */
export const CONSENT_SCRIPT_VERSION = "1";
export const CONSENT_SCRIPT_DATE = "September 27, 2026";

const CONSENT_PAGE = "https://ea-ai-projects.github.io/small-business-scheduling-assistant/sms-consent/";

/**
 * Record that a client clearly said yes in person to receiving texts. No text is sent.
 * Remounted by key when the selection or the client's data changes.
 */
export function ConsentForm({ client, onboarding = false, onSkip }: {
  client: ClientProfile | undefined;
  /** True right after the owner created this client: show the step as part of onboarding. */
  onboarding?: boolean;
  onSkip?: () => void;
}) {
  const { change, notify } = useOwner();
  const [clearYes, setClearYes] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!client) { notify("Select a client first", true); return; }
    if (!clearYes) { notify("Record consent only after they clearly said yes", true); return; }
    const body: InPersonConsentBody = {
      phone_e164: client.phone_e164,
      participant_name: client.name,
      script_version: CONSENT_SCRIPT_VERSION,
      clear_yes: true,
    };
    if (await change(path`/clients/${client.client_id}/sms-consent`, "POST", body,
      "Text consent recorded", false)) setClearYes(false);
  }

  return (
    <section className="card">
      <h3>{onboarding ? "Onboarding: record in-person text consent" : "Record in-person text consent"}</h3>
      {onboarding && (
        <p className="hint" role="status">
          Last step for this new client. Recording consent also marks their phone verified, which
          lets them text. You can skip it now; until it is recorded, this client cannot text.{" "}
          <button type="button" onClick={onSkip}>Skip for now</button>
        </p>
      )}
      <p className="hint">
        Read the exact script on the <a href={CONSENT_PAGE} target="_blank" rel="noreferrer">
        public consent page</a> aloud first. Recording consent sends no text. The full private
        consent record is kept by the owner outside this app.
      </p>
      <form className="form-stack" aria-label="Record in-person text consent" onSubmit={submit}>
        <label>Participant name <input name="participant_name" readOnly
          value={client?.name ?? ""} /></label>
        <p className="hint">In dev this is the profile&apos;s placeholder name; record the real name
          only in your private consent record.</p>
        <p className="hint">Read the number back to them before recording consent.</p>
        <p className="hint">Record this right after they say yes; the time saved is the consent time.</p>
        <p>{`Script version ${CONSENT_SCRIPT_VERSION} (${CONSENT_SCRIPT_DATE})`}</p>
        <label className="inline"><input name="clear_yes" type="checkbox" required
          checked={clearYes} onChange={(event) => setClearYes(event.target.checked)} /> They clearly said yes</label>
        <button className="primary" type="submit" disabled={!client}>Record consent</button>
      </form>
    </section>
  );
}
