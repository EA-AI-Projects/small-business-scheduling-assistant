import type { FormEvent } from "react";

import type { PolicyEditBody } from "@/api/types";
import { useOwner } from "@/owner/OwnerContext";

import { withDateException, type ExceptionMode } from "./policyEdit";

export function ExceptionForm() {
  const { data, change, notify } = useOwner();

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const state = data.policy;
    if (!state) { notify("Configure the policy before editing exceptions", true); return; }
    const form = new FormData(event.currentTarget);
    const policy = withDateException(state.record.policy, String(form.get("date")),
      String(form.get("mode")) as ExceptionMode,
      { opens: String(form.get("opens")), closes: String(form.get("closes")) });
    const body: PolicyEditBody = { expected_revision: state.calendar_revision,
      expected_version: state.record.version, policy };
    await change("/policy", "PUT", body, "Date exception saved");
  }

  return (
    <section className="card">
      <h3>Date exception</h3>
      <p className="hint">Close one date or set custom hours. Existing appointments may block the change.</p>
      <form className="form-stack" aria-label="Date exception" onSubmit={submit}>
        <label>Date <input name="date" type="date" required /></label>
        <label>Hours <select name="mode" defaultValue="closed">
          <option value="closed">Closed</option>
          <option value="custom">Custom hours</option>
          <option value="normal">Restore normal hours</option>
        </select></label>
        <div className="two-col">
          <label>Open <input name="opens" type="time" defaultValue="08:00" /></label>
          <label>Close <input name="closes" type="time" defaultValue="17:00" /></label>
        </div>
        <button className="primary" type="submit">Save exception</button>
      </form>
    </section>
  );
}
