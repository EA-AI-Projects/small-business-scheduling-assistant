import { useEffect, useState } from "react";

import type { Appointment, CalendarEvent, UnavailableBlock } from "@/api/types";
import { path } from "@/lib/api";
import { localInput, localStamp, localTime, statusLabel } from "@/lib/time";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";
import { ConfirmButton } from "../ConfirmButton";

type Detail = { kind: "block"; block: UnavailableBlock } | { kind: "appointment"; appointment: Appointment };

/** Loads the exact current record for one calendar event. Remounted after every refresh. */
export function EventDetail({ event }: { event: CalendarEvent }) {
  const { api, data, notify } = useOwner();
  const [detail, setDetail] = useState<Detail | null>(null);
  const isBlock = event.status === "UNAVAILABLE";

  useEffect(() => {
    let current = true;
    const load = isBlock
      ? api.get<UnavailableBlock>(path`/blocks/${event.event_id}`).then((block): Detail => ({ kind: "block", block }))
      : api.get<Appointment>(path`/appointments/${event.event_id}`)
        .then((appointment): Detail => ({ kind: "appointment", appointment }));
    load.then((value) => { if (current) setDetail(value); })
      .catch((error: unknown) => { if (current) notify(errorMessage(error), true); });
    return () => { current = false; };
  }, [api, event.event_id, isBlock, notify]);

  return (
    <div>
      <p className="badge">{statusLabel(event.status)}</p>
      <p>{localStamp(event.start_at, data.zone)}–{localTime(event.end_at, data.zone)}</p>
      {!detail ? <p className="muted">Loading…</p>
        : detail.kind === "block" ? <BlockEditor block={detail.block} />
          : <AppointmentEditor appointment={detail.appointment} />}
    </div>
  );
}

function BlockEditor({ block }: { block: UnavailableBlock }) {
  const { data, change, resolveLocal, notify } = useOwner();
  const [start, setStart] = useState(() => localInput(block.start_at, data.zone));
  const [end, setEnd] = useState(() => localInput(block.end_at, data.zone));
  const revision = data.calendar?.revision ?? 0;
  const blockPath = path`/blocks/${block.block_id}`;
  return (
    <>
      <label>Start <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></label>
      <label>End <input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
      <div className="row-actions">
        <BusyButton onClick={async () => {
          try {
            await change(blockPath, "PUT", {
              expected_revision: revision, expected_version: block.version,
              start_at: await resolveLocal(start), end_at: await resolveLocal(end),
            }, "Block moved");
          } catch (error) {
            notify(`Nothing was saved: ${errorMessage(error)}`, true);
          }
        }}>Move block</BusyButton>
        <ConfirmButton label="Remove block" confirmation="Confirm removal" className="danger"
          onConfirm={() => change(blockPath, "DELETE",
            { expected_revision: revision, expected_version: block.version }, "Block removed")} />
      </div>
    </>
  );
}

function AppointmentEditor({ appointment }: { appointment: Appointment }) {
  const { data, change, resolveLocal, notify, selectClient, setTab } = useOwner();
  const [start, setStart] = useState(() => localInput(appointment.start_at, data.zone));
  const [duration, setDuration] = useState(String(appointment.duration_minutes));
  const client = data.clients.find((item) => item.client_id === appointment.client_id);
  const appointmentPath = path`/appointments/${appointment.appointment_id}`;
  return (
    <>
      <p className="meta">
        Client {client ? `${client.name} (${appointment.client_id})` : appointment.client_id}
        {" · "}{appointment.duration_minutes} minutes
      </p>
      <button type="button" onClick={() => {
        if (!client) { notify("Client profile is not available", true); return; }
        selectClient(client.client_id, appointment.appointment_id);
        setTab("clients");
      }}>Open client and visit notes</button>
      {appointment.status === "CONFIRMED" && (
        <>
          <label>Start <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></label>
          <label>Duration (minutes){" "}
            <input type="number" min={1} value={duration} onChange={(e) => setDuration(e.target.value)} />
          </label>
          <div className="row-actions">
            <BusyButton onClick={async () => {
              try {
                await change(appointmentPath, "PATCH", {
                  expected_version: appointment.version, start_at: await resolveLocal(start),
                  duration_minutes: Number(duration),
                }, "Appointment saved");
              } catch (error) {
                notify(`Nothing was saved: ${errorMessage(error)}`, true);
              }
            }}>Save appointment</BusyButton>
            <ConfirmButton label="Cancel appointment" confirmation="Confirm cancellation" className="danger"
              onConfirm={() => change(`${appointmentPath}/cancel`, "POST",
                { expected_version: appointment.version }, "Appointment cancelled")} />
          </div>
        </>
      )}
      {appointment.status === "PENDING_APPROVAL" && (
        <p className="hint">Use Requests to approve or decline this exact request.</p>
      )}
    </>
  );
}
