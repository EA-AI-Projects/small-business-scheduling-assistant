import { useState, type FormEvent } from "react";

import { useOwner } from "@/owner/OwnerContext";

import { withDateException, type ExceptionMode } from "./policyEdit";
import { FailureAlert } from "./FailureAlert";
import { usePolicySave } from "./usePolicySave";

export function ExceptionForm() {
  const { data } = useOwner();
  const { save, failure, clearFailure } = usePolicySave("Date exception saved");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setError(null);
    clearFailure();
    const values = new FormData(form);
    const date = String(values.get("date"));
    const mode = String(values.get("mode")) as ExceptionMode;
    const window = { opens: String(values.get("opens")), closes: String(values.get("closes")) };
    if (!date) { setError("Choose a date."); return; }
    if (mode === "custom" && (!window.opens || !window.closes || window.closes <= window.opens)) {
      setError("Closing time must be after opening time.");
      return;
    }
    // A failed save leaves the form as typed; only a committed save clears it.
    if (await save((policy) => withDateException(policy, date, mode, window))) form.reset();
  }

  const exceptions = Object.keys(data.policy?.record.policy.date_exceptions ?? {}).length;
  return (
    <section className="card">
      <h3>Date exception</h3>
      <p className="hint">Close one date or set custom hours, including opening a holiday. Existing appointments
        may block the change. {exceptions} saved {exceptions === 1 ? "exception is" : "exceptions are"} listed in
        the current policy.</p>
      <form className="form-stack" aria-label="Date exception" onSubmit={submit}>
        <label>Date <input name="date" type="date" required aria-invalid={error !== null && error.includes("date")}
          aria-describedby={error ? "date-exception-error" : undefined} /></label>
        <label>Hours <select name="mode" defaultValue="closed">
          <option value="closed">Closed</option>
          <option value="custom">Custom hours</option>
          <option value="normal">Restore normal hours</option>
        </select></label>
        <div className="two-col">
          <label>Open <input name="opens" type="time" defaultValue="08:00" /></label>
          <label>Close <input name="closes" type="time" defaultValue="17:00"
            aria-invalid={error !== null && error.includes("Closing")}
            aria-describedby={error ? "date-exception-error" : undefined} /></label>
        </div>
        {error && <p id="date-exception-error" className="field-error" role="alert">{error}</p>}
        <FailureAlert failure={failure} />
        <button className="primary" type="submit">Save exception</button>
      </form>
    </section>
  );
}
