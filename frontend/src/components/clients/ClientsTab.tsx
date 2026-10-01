import { useState } from "react";

import { useOwner } from "@/owner/OwnerContext";

import { SectionHeading } from "../Workspace";
import { ClientList } from "./ClientList";
import { ConsentForm } from "./ConsentForm";
import { DeliveryFailures } from "./DeliveryFailures";
import { NoteForm } from "./NoteForm";
import { NoteList } from "./NoteList";
import { ProfileForm } from "./ProfileForm";

export function ClientsTab() {
  const { data, stamp, selectedClientId, selectClient, selectionVersion } = useOwner();
  const [blank, setBlank] = useState(0);
  const [phoneDraft, setPhoneDraft] = useState<{ key: string; phone: string } | null>(null);
  const [onboardingId, setOnboardingId] = useState<string | null>(null);
  const client = data.clients.find((item) => item.client_id === selectedClientId);
  const clientId = client?.client_id ?? null;
  const onboarding = client !== undefined && client.client_id === onboardingId
    && client.phone_verified_at === null;
  // Remount the profile form with current values after each refresh, as the old page did.
  const profileKey = client ? `client:${client.client_id}:${stamp.version}` : `new:${blank}`;

  const phoneUnsaved = client !== undefined && phoneDraft?.key === profileKey
    && phoneDraft.phone.trim() !== client.phone_e164;

  return (
    <>
      <SectionHeading eyebrow="CLIENT RECORDS" title="Clients">
        <button type="button" onClick={() => { selectClient(null); setBlank((value) => value + 1); }}>
          New client
        </button>
      </SectionHeading>
      <div className="split">
        <div className="stack">
          <ClientList clients={data.clients} onSelect={(id) => selectClient(id)} />
          <DeliveryFailures />
        </div>
        <div className="stack">
          <ProfileForm key={profileKey} client={client} onCreated={setOnboardingId}
            onPhoneDraft={(phone) => setPhoneDraft({ key: profileKey, phone })} />
          <ConsentForm key={`consent:${profileKey}`} client={client} onboarding={onboarding} phoneUnsaved={phoneUnsaved}
            onSkip={() => setOnboardingId(null)} />
          <section className="card">
            <h3>Notes</h3>
            <p className="hint">Select a client first. Do not enter access codes.</p>
            <NoteList clientId={clientId} />
            <NoteForm key={`${clientId ?? ""}:${selectionVersion}`} clientId={clientId} />
          </section>
        </div>
      </div>
    </>
  );
}
