/**
 * Empty-slot prefill for the "block time" card. Times are business-local wall-clock strings
 * ("YYYY-MM-DDTHH:mm", the datetime-local format); the server resolves them to instants.
 */
import { localInput } from "./time";

export const SLOT_MINUTES = 30;
export const DEFAULT_BLOCK_MINUTES = 60;
/** Start used for the "Block time" control on a day other than today. */
export const DEFAULT_START_MINUTE = 9 * 60;
const SLOTS_PER_DAY = (24 * 60) / SLOT_MINUTES;

export interface SlotRange { start: string; end: string }

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function wallOf(day: string, minute: number): string {
  return `${day}T${pad(Math.floor(minute / 60))}:${pad(minute % 60)}`;
}

/** Offset of a zone (minutes, local minus UTC) at an instant, as the wall clock shows it. */
function offsetAt(instant: number, zone: string): number {
  const local = localInput(new Date(instant).toISOString(), zone);
  return (Date.parse(`${local}:00Z`) - instant) / 60000;
}

/** Instants whose business-local wall clock reads `wall`: none (DST gap), one, or two (repeated hour). */
function instantsFor(wall: string, zone: string): number[] {
  const asUtc = Date.parse(`${wall}:00Z`);
  if (Number.isNaN(asUtc)) return [];
  const offsets = new Set([offsetAt(asUtc - 86_400_000, zone), offsetAt(asUtc + 86_400_000, zone)]);
  return [...offsets].map((offset) => asUtc - offset * 60000)
    .filter((instant) => localInput(new Date(instant).toISOString(), zone) === wall)
    .sort((a, b) => a - b);
}

/** Wall-clock text for `wall` plus whole minutes, by calendar arithmetic (no zone). */
function addWallMinutes(wall: string, minutes: number): string {
  const moved = new Date(Date.parse(`${wall}:00Z`) + minutes * 60000);
  return moved.toISOString().slice(0, 16);
}

/**
 * Default one-hour range starting at a wall-clock slot. A start inside a spring-forward gap does
 * not exist, so it moves to the first real slot after the gap. The end is one real hour after the
 * start, shown on the wall clock (so 01:30 on a spring-forward day ends at 03:30). In the repeated
 * fall-back hour the wall time is ambiguous; the end is one wall-clock hour later, and the server
 * asks the owner to pick an unambiguous time.
 */
export function rangeFrom(startWall: string, zone: string): SlotRange {
  let start = startWall;
  for (let step = 0; step < 8 && instantsFor(start, zone).length === 0; step += 1) {
    start = addWallMinutes(start, SLOT_MINUTES);
  }
  const instants = instantsFor(start, zone);
  const only = instants.length === 1 ? instants[0] : undefined;
  const end = only === undefined
    ? addWallMinutes(start, DEFAULT_BLOCK_MINUTES)
    : localInput(new Date(only + DEFAULT_BLOCK_MINUTES * 60000).toISOString(), zone);
  return { start, end };
}

/**
 * The slot under a press: `fraction` is how far down the day column (0 to 1) the press landed.
 * The column always has 24 hour rows of wall-clock time, so the fraction maps straight to a
 * wall-clock slot start, floored to the 30-minute slot.
 */
export function slotAtFraction(day: string, fraction: number, zone: string): SlotRange {
  const index = Math.min(SLOTS_PER_DAY - 1, Math.max(0, Math.floor(fraction * SLOTS_PER_DAY)));
  return rangeFrom(wallOf(day, index * SLOT_MINUTES), zone);
}

/**
 * Prefill for the "Block time" control. Today: the next 30-minute slot after now (the latest slot
 * of the day if it is late). Another day: 09:00, the start of a typical working morning; this is
 * only a starting value the owner edits, not a scheduling rule.
 */
export function nextSlotOn(day: string, now: Date, zone: string): SlotRange {
  const local = localInput(now.toISOString(), zone);
  if (local.slice(0, 10) !== day) return rangeFrom(wallOf(day, DEFAULT_START_MINUTE), zone);
  const minute = Number(local.slice(11, 13)) * 60 + Number(local.slice(14, 16));
  const next = (Math.floor(minute / SLOT_MINUTES) + 1) * SLOT_MINUTES;
  return rangeFrom(wallOf(day, Math.min(next, 24 * 60 - SLOT_MINUTES)), zone);
}
