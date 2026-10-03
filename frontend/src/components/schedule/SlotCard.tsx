import { useState } from "react";

import type { SlotRange } from "@/lib/slots";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

import { PopoverCard, type CardAnchor } from "../PopoverCard";

/**
 * The card for marking time unavailable, opened from an empty calendar slot or the "Block time"
 * control. The entered times live in the form, which is keyed per opening, so a rejected save
 * (and the refresh after a conflict) leaves them in place for another try.
 */
export function SlotCard({ range, openKey, getAnchor, onClose, onAnchorPress, onSlotPress, returnFocus }: {
  range: SlotRange; openKey: number; getAnchor: () => CardAnchor | null; onClose: () => void;
  onAnchorPress: (element: HTMLElement) => void;
  onSlotPress: (column: HTMLElement, x: number, y: number) => void;
  returnFocus: () => HTMLElement | null;
}) {
  return (
    <PopoverCard title="Block unavailable time" getAnchor={getAnchor} onClose={onClose}
      onAnchorPress={onAnchorPress} onSlotPress={onSlotPress} returnFocus={returnFocus} refocusKey={openKey}>
      <SlotForm key={openKey} range={range} onClose={onClose} />
    </PopoverCard>
  );
}

function SlotForm({ range, onClose }: { range: SlotRange; onClose: () => void }) {
  const { data, change, resolveLocal, notify } = useOwner();
  const [start, setStart] = useState(range.start);
  const [end, setEnd] = useState(range.end);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (busy) return;
    if (!data.calendar) { notify("The calendar has not loaded yet", true); return; }
    setBusy(true);
    try {
      const body = {
        expected_revision: data.calendar.revision,
        start_at: await resolveLocal(start),
        end_at: await resolveLocal(end),
      };
      if (await change("/blocks", "POST", body, "Unavailable time added")) onClose();
    } catch (error) {
      notify(`Nothing was saved: ${errorMessage(error)}`, true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="form-stack" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <label>Start <input type="datetime-local" value={start} required onChange={(e) => setStart(e.target.value)} /></label>
      <label>End <input type="datetime-local" value={end} required onChange={(e) => setEnd(e.target.value)} /></label>
      <div className="row-actions">
        <button className="primary" type="submit" disabled={busy}>Add block</button>
      </div>
    </form>
  );
}
