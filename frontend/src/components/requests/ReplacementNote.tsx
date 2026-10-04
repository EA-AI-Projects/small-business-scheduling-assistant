import type { Appointment } from "@/api/types";
import { localStamp, localTime } from "@/lib/time";

/** Explains how a replacement request relates to the visit it replaces. */
export function ReplacementNote({ request, original, zone }: {
  request: Appointment;
  /** The original when it is itself a pending request; otherwise null. */
  original: Appointment | null;
  zone: string;
}) {
  if (!request.replaces_appointment_id) return null;
  if (original) {
    return (
      <p className="hint">
        Replacement request; the original request for {localStamp(original.start_at, zone)}–
        {localTime(original.end_at, zone)} is also pending. Approving this replacement resolves both requests.
      </p>
    );
  }
  return <p className="hint">Replacement request; original visit stays until approval.</p>;
}
