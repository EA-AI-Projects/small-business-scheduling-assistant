import { useCallback, useEffect, useRef, useState } from "react";

import { useOwner } from "@/owner/OwnerContext";

import { PopoverCard } from "../PopoverCard";
import { SectionHeading } from "../Workspace";
import { ClientDetails } from "./ClientDetails";
import { ClientList } from "./ClientList";
import { DeliveryFailures } from "./DeliveryFailures";

/** What the pop-up shows. `token` changes per opening, so the sections reset only then. */
type Open = { token: number; clientId: string | null; section: "profile" | "notes" };

export function ClientsTab() {
  const { data, stamp, selectClient, notesRequest, clearNotesRequest } = useOwner();
  const [open, setOpen] = useState<Open | null>(null);
  const tokens = useRef(0);
  const newButton = useRef<HTMLButtonElement>(null);
  const client = open?.clientId ? data.clients.find((item) => item.client_id === open.clientId) : undefined;

  // "Open client and visit notes" from a calendar card lands here with a request to take.
  useEffect(() => {
    if (notesRequest === null) return;
    clearNotesRequest();
    tokens.current += 1;
    setOpen({ token: tokens.current, clientId: notesRequest, section: "notes" });
  }, [notesRequest, clearNotesRequest]);

  const openClient = (clientId: string | null) => {
    selectClient(clientId);
    tokens.current += 1;
    setOpen({ token: tokens.current, clientId, section: "profile" });
  };

  const openId = open?.clientId;
  const returnFocus = useCallback(() => {
    // Looked up at close time: the list may have re-rendered while the pop-up was open.
    return (openId ? document.querySelector<HTMLElement>(`[data-client-id="${CSS.escape(openId)}"]`) : null)
      ?? newButton.current;
  }, [openId]);
  const getAnchor = useCallback(() => null, []);

  // The record went away while open: nothing left to show.
  // Clear the open state too, so the pop-up cannot reappear later from another tab.
  const gone = openId != null && client === undefined;
  if (gone) setOpen(null);

  return (
    <>
      <SectionHeading eyebrow="CLIENT RECORDS" title="Clients">
        <button type="button" ref={newButton} aria-haspopup="dialog" onClick={() => openClient(null)}>
          New client
        </button>
      </SectionHeading>
      <div className="stack">
        <ClientList clients={data.clients} onSelect={openClient} />
        <DeliveryFailures />
      </div>
      {open && !gone && (
        <PopoverCard variant="modal" title={client?.name ?? "New client"} getAnchor={getAnchor}
          onClose={() => setOpen(null)} returnFocus={returnFocus}
          refocusKey={`${open.clientId ?? "new"}:${stamp.version}`}>
          <p className="eyebrow">CLIENT DETAILS</p>
          <ClientDetails key={open.token} client={client} blankKey={open.token}
            initialSection={open.section}
            onCreated={(id) => setOpen((current) => current && { ...current, clientId: id })} />
        </PopoverCard>
      )}
    </>
  );
}
