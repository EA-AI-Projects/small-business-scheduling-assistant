import { useEffect, useState, type FormEvent } from "react";

import { path } from "@/lib/api";
import { useOwner } from "@/owner/OwnerContext";

type AccountStatus = { state: string; expires_at: string | null };

/** Email account access is separate from the client's phone and text consent. */
export function AccountInvitationForm({ clientId }: { clientId: string }) {
  const { api, change, notify } = useOwner();
  const [email, setEmail] = useState("");
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState<AccountStatus | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const route = path`/clients/${clientId}/account-invitation`;

  useEffect(() => {
    let current = true;
    api.get<AccountStatus>(route)
      .then((value) => { if (current) setStatus(value); })
      .catch((error: unknown) => { if (current) notify(error instanceof Error ? error.message : String(error), true); });
    return () => { current = false; };
  }, [api, route, notify]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sending) return;
    setSending(true);
    try {
      if (await change(route, "POST", { email },
        "Account invitation requested. It expires in 24 hours.", false)) {
        setStatus(await api.get<AccountStatus>(route));
      }
    } finally {
      setSending(false);
    }
  }

  async function revoke() {
    setSending(true);
    try {
      if (await change(route, "DELETE", undefined, "Account access revoked", false)) {
        setStatus(await api.get<AccountStatus>(route));
        setConfirmRevoke(false);
      }
    } finally {
      setSending(false);
    }
  }

  return (
    <form className="form-stack" aria-label="Client account invitation" onSubmit={submit}>
      <p className="hint">Invite this client to sign in by email. This does not verify their phone or grant text consent.</p>
      {status && status.state !== "none" && <p className="hint">Account: {status.state}
        {status.state === "pending" && status.expires_at
          ? ` (expires ${new Date(status.expires_at).toLocaleString()})` : ""}.</p>}
      <label>Email <input type="email" autoComplete="email" required maxLength={320}
        value={email} onChange={(event) => setEmail(event.target.value)} /></label>
      <button type="submit" disabled={sending}>{sending ? "Inviting…" : "Send email invitation"}</button>
      {status && ["pending", "active"].includes(status.state) && (
        <div className="stack">
          {!confirmRevoke ? <button type="button" disabled={sending} onClick={() => setConfirmRevoke(true)}>
            Revoke account access
          </button> : <div className="row-actions">
            <button type="button" className="danger" disabled={sending} onClick={revoke}>Confirm revoke</button>
            <button type="button" disabled={sending} onClick={() => setConfirmRevoke(false)}>Keep access</button>
          </div>}
        </div>
      )}
    </form>
  );
}
