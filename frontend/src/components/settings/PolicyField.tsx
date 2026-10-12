import type { ReactNode } from "react";

/** A labelled control with its error linked by aria-describedby and aria-invalid. */
export function PolicyField({ id, label, error, shown, hint, children }: {
  id: string; label: string; error: string | undefined; shown: boolean; hint?: string;
  children: (props: { id: string; "aria-invalid": boolean; "aria-describedby": string | undefined }) => ReactNode;
}) {
  const showError = shown && error !== undefined;
  const described = [showError ? `${id}-error` : null, hint ? `${id}-hint` : null].filter(Boolean).join(" ") || undefined;
  return <div className="form-field">
    <label htmlFor={id}>{label}</label>
    {children({ id, "aria-invalid": showError, "aria-describedby": described })}
    {showError && <p id={`${id}-error`} className="field-error">{error}</p>}
    {hint && <p id={`${id}-hint`} className="hint">{hint}</p>}
  </div>;
}
