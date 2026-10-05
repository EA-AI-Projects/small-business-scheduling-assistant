import { useEffect, useState } from "react";

import { useOwner } from "@/owner/OwnerContext";

const ARM_TIMEOUT_MS = 5000;

/** Two-step button: the first click arms it, the second performs the action. */
export function ConfirmButton({ label, confirmation, onConfirm, className = "" }: {
  label: string;
  confirmation: string;
  onConfirm: () => Promise<unknown> | void;
  className?: string;
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const { tab } = useOwner();

  useEffect(() => {
    if (!armed) return;
    const timeout = window.setTimeout(() => setArmed(false), ARM_TIMEOUT_MS);
    return () => window.clearTimeout(timeout);
  }, [armed]);

  // Sections stay mounted while hidden, so leaving one must clear its pending confirmation.
  useEffect(() => () => setArmed(false), [tab]);

  return (
    <button type="button" className={className} disabled={busy}
      onBlur={() => setArmed(false)}
      onKeyDown={(event) => { if (event.key === "Escape") setArmed(false); }}
      onClick={async () => {
      if (!armed) { setArmed(true); return; }
      setBusy(true);
      try { await onConfirm(); } finally { setBusy(false); setArmed(false); }
    }}>
      {armed ? confirmation : label}
    </button>
  );
}
