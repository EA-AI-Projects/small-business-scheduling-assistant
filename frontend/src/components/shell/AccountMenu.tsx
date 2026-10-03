import { useCallback, useRef, useState } from "react";

import { PopoverCard } from "../PopoverCard";

function PersonIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
      <circle cx="12" cy="8" r="4" fill="currentColor" />
      <path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7z" fill="currentColor" />
    </svg>
  );
}

/** Round avatar: the first letter of the email, or a person icon when there is none. */
function Avatar({ initial, large = false }: { initial: string | null; large?: boolean }) {
  return (
    <span className={`avatar${large ? " large" : ""}`} aria-hidden="true">
      {initial ?? <PersonIcon />}
    </span>
  );
}

/**
 * Account button for the header and its pop-up: who is signed in, then rows of actions. Today the
 * only row is Sign out; further rows (for example Preferences) go in the same `.account-actions` list.
 */
export function AccountMenu({ email, local, onSignOut }: {
  email: string | null;
  local: boolean;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const getAnchor = useCallback(() => button.current, []);
  const returnFocus = useCallback(() => button.current, []);
  const initial = !local && email ? (Array.from(email)[0] ?? "").toLocaleUpperCase() || null : null;
  const identity = local ? "Local owner (synthetic)" : email ?? "Signed in";
  return (
    <>
      <button ref={button} type="button" className="avatar-button" aria-haspopup="dialog" aria-expanded={open}
        aria-label={!local && email ? `Account: ${email}` : "Account"}
        // While the pop-up is open the header is inert, so a press here is an outside press: it
        // closes the pop-up and the click never reaches this button, so it cannot reopen.
        onClick={() => setOpen(true)}>
        <Avatar initial={initial} />
      </button>
      {open && (
        <PopoverCard title="Account" getAnchor={getAnchor} returnFocus={returnFocus} variant="dropdown"
          align="end" onClose={() => setOpen(false)}>
          <div className="account-identity">
            <Avatar initial={initial} large />
            <span className="account-name">{identity}</span>
          </div>
          <ul className="account-actions">
            <li>
              <button type="button" className="account-row" onClick={() => { setOpen(false); onSignOut(); }}>
                <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
                  <path d="M10 4H5v16h5M15 8l4 4-4 4M19 12H9" fill="none" stroke="currentColor" strokeWidth="2"
                    strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                <span>Sign out</span>
              </button>
            </li>
          </ul>
        </PopoverCard>
      )}
    </>
  );
}
