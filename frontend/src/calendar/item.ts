/**
 * What the shared calendar views draw: an identified span of time with a status. It carries
 * nothing owner-specific (no holds, buffers, or durations), so the owner and client apps can
 * both map their own records onto it. `status` uses the calendar status vocabulary; its
 * lowercase first word is the colour kind and the status with underscores spaced is the label.
 */
export interface CalendarItem {
  event_id: string;
  start_at: string;
  end_at: string;
  status: string;
}

/** The colour class suffix for an item: CONFIRMED -> "confirmed", PENDING_APPROVAL -> "pending". */
export function itemKind(item: CalendarItem): string {
  return item.status.toLowerCase().split("_")[0] ?? "";
}
