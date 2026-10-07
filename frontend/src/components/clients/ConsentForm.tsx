import { useState, type FormEvent } from "react";

import type { ClientProfile, InPersonConsentBody } from "@/api/types";
import { path } from "@/lib/api";
import { dayKey } from "@/lib/time";
import { useOwner } from "@/owner/OwnerContext";

/**
 * The consent script the owner reads aloud. Keep in step with the "Script version" line in
 * docs/sms-consent/index.html (checked by ConsentScript.test.ts).
 */
export const CONSENT_SCRIPT_VERSION = "2";
export const CONSENT_SCRIPT_DATE = "October 7, 2026";

const CONSENT_PAGE = "https://ea-ai-projects.github.io/small-business-scheduling-assistant/sms-consent/";

/**
 * Record that a client clearly said yes in person to receiving texts. Recording consent for a
 * client's first enrollment also sends one welcome text; later records send none.
 * Remounted by key when the selection or the client's data changes.
 */
export function ConsentForm({ client, onboarding = false, phoneUnsaved = false, onSkip }: {
  client: ClientProfile | undefined;
  /** True right after the owner created this client: show the step as part of onboarding. */
  onboarding?: boolean;
  /** The profile form's phone differs from the saved one; consent must wait for a save. */
  phoneUnsaved?: boolean;
  onSkip?: () => void;
}) {
  const { change, notify, data } = useOwner();
  const [clearYes, setClearYes] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!client) { notify("Select a client first", true); return; }
    if (phoneUnsaved) { notify("Save the profile first", true); return; }
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
    <div className="stack">
      {client && (
        <p className="hint" role="status" data-testid="consent-status">
          {client.phone_verified_at
            ? `Consent recorded on ${dayKey(client.phone_verified_at, data.zone)} for ${client.phone_e164}.`
            : "No consent recorded for this phone."}
        </p>
      )}
      <h5>{onboarding ? "Onboarding: record in-person text consent" : "Record in-person text consent"}</h5>
      {onboarding && (
        <p className="hint" role="status">
          Last step for this new client. Recording consent also marks their phone verified, which
          lets them text. You can record it later from this screen; until then, this client cannot text.{" "}
          <button type="button" onClick={onSkip}>Record later</button>
        </p>
      )}
      <p className="hint">
        Read the exact script on the <a href={CONSENT_PAGE} target="_blank" rel="noreferrer">
        public consent page</a> aloud first. After the first successful consent for a new client, one
        welcome text is sent to the saved phone. Recording consent again for an existing client
        sends no text. The full private consent record is kept by the owner outside this app.
      </p>
      <form className="form-stack" aria-label="Record in-person text consent" onSubmit={submit}>
        <label>Participant name <input name="participant_name" readOnly
          value={client?.name ?? ""} /></label>
        <p className="hint">In dev this is the profile&apos;s placeholder name; record the real name
          only in your private consent record.</p>
        <label>Saved phone number <input name="saved_phone" readOnly
          value={client?.phone_e164 ?? ""} /></label>
        <p className="hint">{`Read this number back to them: ${client?.phone_e164 ?? ""}`}</p>
        {phoneUnsaved && (
          <p className="hint" role="alert">
            The phone has unsaved changes. Save the profile first; consent is recorded for the
            saved number.
          </p>
        )}
        <p className="hint">Record this right after they say yes; the time saved is the consent time.</p>
        <p>{`Script version ${CONSENT_SCRIPT_VERSION} (${CONSENT_SCRIPT_DATE})`}</p>
        <label className="inline"><input name="clear_yes" type="checkbox" required
          checked={clearYes} onChange={(event) => setClearYes(event.target.checked)} /> They clearly said yes</label>
        <button className="primary" type="submit" disabled={!client || phoneUnsaved}>Record consent</button>
      </form>
    </div>
  );
}
