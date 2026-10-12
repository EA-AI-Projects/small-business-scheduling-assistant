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
      <p>The saved values above are now current. Your edits are kept; review them and save again to apply them
        on top of the newer policy.</p>
    </div>;
  }
  return <p className="field-error" role="alert">{`Nothing was saved: ${failure.message}`}</p>;
}
