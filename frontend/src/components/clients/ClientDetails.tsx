import { useId, useState, type ReactNode } from "react";

import type { ClientProfile } from "@/api/types";
import { normalizePhone } from "@/lib/phone";
import { useOwner } from "@/owner/OwnerContext";

import { ConsentForm } from "./ConsentForm";
import { NoteForm } from "./NoteForm";
import { NoteList } from "./NoteList";
import { ProfileForm } from "./ProfileForm";

type SectionName = "profile" | "consent" | "notes";

function Accordion({ title, open, onToggle, disabledReason, children }: {
  title: string;
  open: boolean;
  onToggle: () => void;
  /** When set, the section cannot be opened and this says why. */
  disabledReason?: string;
  children: ReactNode;
}) {
  const id = useId();
  const disabled = disabledReason !== undefined;
  const expanded = open && !disabled;
  return (
    <section className="accordion">
      <h4 className="accordion-title">
        <button type="button" id={`${id}-header`} className="accordion-header" aria-expanded={expanded}
          aria-controls={`${id}-panel`} disabled={disabled}
          aria-describedby={disabled ? `${id}-reason` : undefined} onClick={onToggle}>
          <span>{title}</span>
          <span className="accordion-chevron" aria-hidden="true">{expanded ? "▾" : "▸"}</span>
        </button>
      </h4>
      {disabled && <p id={`${id}-reason`} className="hint accordion-reason">{disabledReason}</p>}
      {/* Kept mounted while collapsed so unsaved entries in a section are not lost. */}
      <div id={`${id}-panel`} role="region" aria-labelledby={`${id}-header`} hidden={!expanded}
        className="accordion-panel">
        {children}
      </div>
    </section>
  );
}

/**
 * The body of the Client details pop-up: Profile, Text Consent and Notes as accordion sections.
 * Mounted once per opening (keyed by the parent), so the expanded sections survive refreshes.
 * `client` is undefined while a new profile has not been saved yet.
 */
export function ClientDetails({ client, blankKey, initialSection, onCreated }: {
  client: ClientProfile | undefined;
  blankKey: number;
  initialSection: SectionName;
  /** Called with the new client's id once its first save succeeds. */
  onCreated: (clientId: string) => void;
}) {
  const { stamp } = useOwner();
  const [open, setOpen] = useState<Record<SectionName, boolean>>({
    profile: initialSection === "profile", consent: initialSection === "consent",
    notes: initialSection === "notes",
  });
  const [phoneDraft, setPhoneDraft] = useState<{ key: string; phone: string } | null>(null);
  const [onboardingId, setOnboardingId] = useState<string | null>(null);
  const clientId = client?.client_id ?? null;
  const onboarding = client !== undefined && client.client_id === onboardingId
    && client.phone_verified_at === null;
  // Remount the profile form with current values after each refresh, as the old page did.
  const profileKey = client ? `client:${client.client_id}:${stamp.version}` : `new:${blankKey}`;
  const phoneUnsaved = client !== undefined && phoneDraft?.key === profileKey
    && normalizePhone(phoneDraft.phone) !== client.phone_e164;
  const toggle = (name: SectionName) => setOpen((current) => ({ ...current, [name]: !current[name] }));
  const waiting = "Save the profile first.";

  return (
    <div className="accordion-stack">
      <Accordion title="Profile" open={open.profile} onToggle={() => toggle("profile")}>
        <ProfileForm key={profileKey} client={client}
          onCreated={(id) => {
            setOnboardingId(id);
            setOpen((current) => ({ ...current, consent: true }));
            onCreated(id);
          }}
          onPhoneDraft={(phone) => setPhoneDraft({ key: profileKey, phone })} />
      </Accordion>
      <Accordion title="Text Consent" open={open.consent} onToggle={() => toggle("consent")}
        disabledReason={client ? undefined : `${waiting} Text consent is recorded for a saved client.`}>
        {client && (
          <ConsentForm key={`consent:${profileKey}`} client={client} onboarding={onboarding}
            phoneUnsaved={phoneUnsaved} onSkip={() => setOnboardingId(null)} />
        )}
      </Accordion>
      <Accordion title="Notes" open={open.notes} onToggle={() => toggle("notes")}
        disabledReason={client ? undefined : `${waiting} Notes are kept for a saved client.`}>
        <div className="stack">
          <p className="hint">Do not enter access codes.</p>
          <NoteList clientId={clientId} />
          <NoteForm key={`${clientId ?? ""}:${blankKey}`} clientId={clientId} />
        </div>
      </Accordion>
    </div>
  );
}
