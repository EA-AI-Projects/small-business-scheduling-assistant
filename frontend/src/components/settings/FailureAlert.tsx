import type { SaveFailure } from "./usePolicySave";

export function FailureAlert({ failure }: { failure: SaveFailure }) {
  if (!failure) return null;
  if (failure.kind === "conflict") {
    return <div className="field-error" role="alert">
      <p>Nothing was saved. This change conflicts with an active booking or pending request:
        {" "}{failure.message}.</p>
      <p>Your saved policy is unchanged and your edits are kept. Move or cancel the visit, or decide the
        request, then save again, or adjust your edit.</p>
    </div>;
  }
  if (failure.kind === "stale") {
    return <div className="field-error" role="alert">
      <p>Nothing was saved. The policy or calendar changed since you opened it ({failure.message}).</p>
      {failure.reloaded === false
        ? <p>The newer policy could not be loaded, so the saved values shown may be out of date. Your edits are
          kept. Check your connection, then save again to reload and review before applying them.</p>
        : <p>Fields you did not change now show the newer saved values. Your own edits are kept; review them
          and save again.</p>}
    </div>;
  }
  return <p className="field-error" role="alert">{`Nothing was saved: ${failure.message}`}</p>;
}

/** Fields the owner changed that were also changed by a newer save; the owner's value wins if saved. */
export function ConflictNotice({ labels }: { labels: string[] }) {
  if (labels.length === 0) return null;
  return <p className="field-error" role="status">
    {`Also changed elsewhere, still showing your value: ${labels.join(", ")}. Saving replaces the newer value; Discard my edits shows it instead.`}
  </p>;
}
