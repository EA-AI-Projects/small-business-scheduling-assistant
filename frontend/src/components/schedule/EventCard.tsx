import type { ReactNode } from "react";

import { itemLabel, type CalendarItem } from "@/calendar/item";
import { localStamp, localTime } from "@/lib/time";

import { PopoverCard, type CardAnchor } from "../PopoverCard";

/**
 * The pop-up card for one calendar item: status, time range, and whatever the caller puts
 * inside (owner record editors, or a client's own booking). It knows nothing about who is
 * looking, so owner-only actions are supplied as `children`.
 */
export function EventCard({ item, zone, title, getAnchor, onClose, onAnchorPress, onSlotPress, returnFocus,
  refocusKey, children }: {
  item: CalendarItem; zone: string; title: string; getAnchor: () => CardAnchor | null; onClose: () => void;
  onAnchorPress: (element: HTMLElement) => void;
  onSlotPress: (column: HTMLElement, x: number, y: number) => void; returnFocus: () => HTMLElement | null;
  refocusKey: string; children?: ReactNode;
}) {
  return (
    <PopoverCard title={title} getAnchor={getAnchor} onClose={onClose} onAnchorPress={onAnchorPress}
      onSlotPress={onSlotPress} returnFocus={returnFocus} refocusKey={refocusKey}>
      <div>
        <p className="badge">{itemLabel(item)}</p>
        <p>{localStamp(item.start_at, zone)}–{localTime(item.end_at, zone)}</p>
        {children}
      </div>
    </PopoverCard>
  );
}
