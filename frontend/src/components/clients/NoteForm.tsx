import { useState, type FormEvent } from "react";

import type { ClientNoteBody } from "@/api/types";
import { path } from "@/lib/api";
import { useOwner } from "@/owner/OwnerContext";

export function NoteForm({ clientId }: { clientId: string | null }) {
  const { change, notify, noteAppointmentId } = useOwner();
  const [body, setBody] = useState("");
  // Remounted on every client selection (see ClientsTab), so a visit opened from the
  // schedule prefills once and never carries over to another client.
  const [appointmentId, setAppointmentId] = useState(noteAppointmentId ?? "");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!clientId) { notify("Select a client first", true); return; }
    const note: ClientNoteBody = { body: body.trim(), appointment_id: appointmentId.trim() || null };
    if (await change(path`/clients/${clientId}/notes`, "POST", note, "Note saved")) {
      setBody("");
      setAppointmentId("");
    }
  }

  return (
    <form className="form-stack" aria-label="Add note" onSubmit={submit}>
      <label>Note <textarea name="body" maxLength={2000} required value={body}
        onChange={(event) => setBody(event.target.value)} /></label>
      <label>Appointment ID (optional) <input name="appointment_id" value={appointmentId}
        onChange={(event) => setAppointmentId(event.target.value)} /></label>
      <button type="submit">Add note</button>
    </form>
  );
}
