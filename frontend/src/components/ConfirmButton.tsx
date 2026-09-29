import { useState } from "react";

/** Two-step button: the first click arms it, the second performs the action. */
export function ConfirmButton({ label, confirmation, onConfirm, className = "" }: {
  label: string;
  confirmation: string;
  onConfirm: () => Promise<unknown> | void;
  className?: string;
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  return (
    <button type="button" className={className} disabled={busy} onClick={async () => {
      if (!armed) { setArmed(true); return; }
      setBusy(true);
      try { await onConfirm(); } finally { setBusy(false); setArmed(false); }
    }}>
      {armed ? confirmation : label}
    </button>
  );
}
