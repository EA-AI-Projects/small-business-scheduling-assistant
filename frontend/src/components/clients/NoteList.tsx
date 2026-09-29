import { useEffect, useState } from "react";

import type { ClientNote } from "@/api/types";
import { path } from "@/lib/api";
import { localStamp } from "@/lib/time";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

import { ConfirmButton } from "../ConfirmButton";

/** Ordinary notes for one client, reloaded after every refresh. Late responses are dropped. */
export function NoteList({ clientId }: { clientId: string | null }) {
  const { api, stamp, data, change, notify } = useOwner();
  const [loaded, setLoaded] = useState<{ clientId: string; notes: ClientNote[] } | null>(null);

  useEffect(() => {
    if (!clientId) return;
    let current = true;
    api.get<ClientNote[]>(path`/clients/${clientId}/notes`)
      .then((notes) => { if (current) setLoaded({ clientId, notes }); })
      .catch((error: unknown) => { if (current) notify(errorMessage(error), true); });
    return () => { current = false; };
  }, [api, clientId, stamp.version, notify]);

  const notes = clientId && loaded?.clientId === clientId ? loaded.notes : null;
  return (
    <div className="card-list" aria-label="Client notes">
      {notes?.length === 0 && <p className="empty">No ordinary notes</p>}
      {notes?.map((note) => (
        <div key={note.note_id} className="note-row">
          <p>{note.body}</p>
          <p className="meta">
            {`${note.appointment_id ? `Visit ${note.appointment_id} · ` : "Client note · "}${
              localStamp(note.created_at, data.zone)}`}
          </p>
          {note.legal_hold_reason && <p className="badge">Legal hold</p>}
          <ConfirmButton label="Delete" confirmation="Confirm deletion" className="danger"
            onConfirm={() => change(path`/clients/${note.client_id}/notes/${note.note_id}`, "DELETE",
              undefined, "Note deleted", false)} />
        </div>
      ))}
    </div>
  );
}
