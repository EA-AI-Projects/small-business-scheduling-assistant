import { useState } from "react";

interface State<T> { base: T | null; draft: T | null; conflicts: string[]; settling?: string }

/**
 * A form draft that starts from the saved value and remembers it as the base. When a newer saved
 * value arrives (a save elsewhere, a refresh, a stale rejection), fields the owner has not touched
 * follow the newer saved value, fields the owner changed are kept, and fields changed both by the
 * owner and by the newer save are kept but flagged in `conflicts` instead of silently overwritten.
 */
export function useSyncedDraft<T extends object>(saved: T | null): {
  draft: T | null; setDraft: (update: (current: T) => T) => void; dirty: boolean; reset: () => void;
  conflicts: string[]; rebase: (committed: T) => void;
} {
  const savedKey = JSON.stringify(saved);
  const [state, setState] = useState<State<T>>({ base: saved, draft: saved, conflicts: [] });
  // While the owner's own committed save is still loading back, the old saved value is not a newer change.
  if (savedKey !== JSON.stringify(state.base) && state.settling !== savedKey) {
    setState(merge(state, saved));
  }
  const draft = state.draft ?? saved;
  const live = state.conflicts.filter((key) =>
    saved !== null && draft !== null && same(draft, saved, key) === false);
  return {
    draft,
    setDraft: (update) => setState((current) => current.draft === null ? current
      : { ...current, draft: update(current.draft) }),
    dirty: saved !== null && JSON.stringify(draft) !== savedKey,
    reset: () => setState({ base: saved, draft: saved, conflicts: [] }),
    conflicts: live,
    // The owner's own save replaced the saved values: they are the new starting point, and every flag is resolved.
    rebase: (committed) => setState((current) => ({ ...current, base: committed, conflicts: [], settling: savedKey })),
  };
}

function same<T extends object>(a: T, b: T, key: string): boolean {
  return JSON.stringify(a[key as keyof T]) === JSON.stringify(b[key as keyof T]);
}

function merge<T extends object>(state: State<T>, saved: T | null): State<T> {
  const { base, draft } = state;
  if (saved === null || base === null || draft === null) return { base: saved, draft: saved, conflicts: [] };
  const next = { ...draft };
  const conflicts = new Set(state.conflicts);
  for (const key of Object.keys(saved) as (keyof T & string)[]) {
    if (same(draft, saved, key)) continue;
    if (same(draft, base, key)) next[key] = saved[key];
    else if (!same(saved, base, key)) conflicts.add(key);
  }
  return { base: saved, draft: next, conflicts: [...conflicts] };
}
