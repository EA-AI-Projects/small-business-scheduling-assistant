import { useState } from "react";

/**
 * A form draft that follows the saved value while the owner has not edited it, and keeps the
 * owner's input when the saved value changes underneath (a save of another form, a refresh, or a
 * newer version after a stale save).
 */
export function useSyncedDraft<T>(saved: T | null): {
  draft: T | null; setDraft: (update: (current: T) => T) => void; dirty: boolean; reset: () => void;
} {
  const savedKey = JSON.stringify(saved);
  const [synced, setSynced] = useState<string>(savedKey);
  const [draft, setDraftState] = useState<T | null>(saved);
  if (savedKey !== synced) {
    // Adjusting state during render: only follow the saved value when nothing was edited.
    setSynced(savedKey);
    if (JSON.stringify(draft) === synced) setDraftState(saved);
  }
  return {
    draft: draft ?? saved,
    setDraft: (update) => setDraftState((current) => update((current ?? saved) as T)),
    dirty: saved !== null && JSON.stringify(draft ?? saved) !== savedKey,
    reset: () => setDraftState(saved),
  };
}
