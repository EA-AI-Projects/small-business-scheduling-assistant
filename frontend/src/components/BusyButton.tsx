import { useState, type ReactNode } from "react";

/** Disabled while its action runs, so a double click cannot send a second write. */
export function BusyButton({ onClick, className = "", children }: {
  onClick: () => Promise<unknown>; className?: string; children: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button type="button" className={className} disabled={busy} onClick={async () => {
      setBusy(true);
      try { await onClick(); } finally { setBusy(false); }
    }}>
      {children}
    </button>
  );
}
