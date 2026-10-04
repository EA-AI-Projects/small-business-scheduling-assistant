import type { Appointment } from "@/api/types";
import { localStamp, localTime } from "@/lib/time";

/** Explains how a replacement request relates to the visit it replaces. */
export function ReplacementNote({ request, original, originalConfirmed, zone }: {
  request: Appointment;
  /** The original when it is itself a pending request; otherwise null. */
  original: Appointment | null;
  /** True only when the original is a confirmed visit on the calendar. */
  originalConfirmed: boolean;
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
  if (originalConfirmed) {
    return <p className="hint">Replacement request; original visit stays until approval.</p>;
  }
  return (
    <p className="hint">
      Replacement request. The original request is no longer pending; approving confirms only this replacement.
    </p>
  );
}
