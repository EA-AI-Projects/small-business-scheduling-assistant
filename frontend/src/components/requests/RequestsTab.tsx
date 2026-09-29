import type { Appointment } from "@/api/types";
import { path } from "@/lib/api";
import { localStamp, localTime } from "@/lib/time";
import { useOwner } from "@/owner/OwnerContext";

import { ConfirmButton } from "../ConfirmButton";
import { SectionHeading } from "../Workspace";

export function RequestsTab() {
  const { data } = useOwner();
  return (
    <>
      <SectionHeading eyebrow="APPROVALS" title="Pending requests" />
      <div className="card-list">
        {data.requests.length === 0 && <p className="empty">No pending requests</p>}
        {data.requests.map((request) => <RequestCard key={request.appointment_id} request={request} />)}
      </div>
    </>
  );
}

function RequestCard({ request }: { request: Appointment }) {
  const { data, change } = useOwner();
  const client = data.clients.find((item) => item.client_id === request.client_id);
  const decide = (action: "approve" | "decline") => change(
    path`/requests/${request.appointment_id}/` + action, "POST",
    { expected_version: request.version }, `Request ${action === "approve" ? "approved" : "declined"}`);
  return (
    <article className="request">
      <h3>{localStamp(request.start_at, data.zone)}–{localTime(request.end_at, data.zone)}</h3>
      <p className="meta">
        Client {client ? `${client.name} (${request.client_id})` : request.client_id}
        {" · "}{request.duration_minutes} minutes
        {request.hold_expires_at && ` · expires ${localStamp(request.hold_expires_at, data.zone)}`}
      </p>
      {request.replaces_appointment_id && (
        <p className="hint">Replacement request; original visit stays until approval.</p>
      )}
      <div className="row-actions">
        <ConfirmButton label="Approve" confirmation="Confirm approval" className="primary"
          onConfirm={() => decide("approve")} />
        <ConfirmButton label="Decline" confirmation="Confirm decline" className="danger"
          onConfirm={() => decide("decline")} />
      </div>
    </article>
  );
}
