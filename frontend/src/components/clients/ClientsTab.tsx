import { useState } from "react";

import { useOwner } from "@/owner/OwnerContext";

import { SectionHeading } from "../Workspace";
import { ClientList } from "./ClientList";
import { NoteForm } from "./NoteForm";
import { NoteList } from "./NoteList";
import { ProfileForm } from "./ProfileForm";

export function ClientsTab() {
  const { data, stamp, selectedClientId, selectClient, selectionVersion } = useOwner();
  const [blank, setBlank] = useState(0);
  const client = data.clients.find((item) => item.client_id === selectedClientId);
  const clientId = client?.client_id ?? null;
  // Remount the profile form with current values after each refresh, as the old page did.
  const profileKey = client ? `client:${client.client_id}:${stamp.version}` : `new:${blank}`;

  return (
    <>
      <SectionHeading eyebrow="CLIENT RECORDS" title="Clients">
        <button type="button" onClick={() => { selectClient(null); setBlank((value) => value + 1); }}>
          New client
        </button>
      </SectionHeading>
      <div className="split">
        <ClientList clients={data.clients} onSelect={(id) => selectClient(id)} />
        <div className="stack">
          <ProfileForm key={profileKey} client={client} />
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
