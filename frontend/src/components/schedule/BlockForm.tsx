import { errorMessage, useOwner } from "@/owner/OwnerContext";

export function BlockForm() {
  const { data, change, resolveLocal, notify } = useOwner();
  return (
    <form className="form-stack" onSubmit={async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const values = new FormData(form);
      if (!data.calendar) { notify("The calendar has not loaded yet", true); return; }
      try {
        const body = {
          expected_revision: data.calendar.revision,
          start_at: await resolveLocal(String(values.get("start") ?? "")),
          end_at: await resolveLocal(String(values.get("end") ?? "")),
        };
        if (await change("/blocks", "POST", body, "Unavailable time added")) form.reset();
      } catch (error) {
        notify(`Nothing was saved: ${errorMessage(error)}`, true);
      }
    }}>
      <label>Start <input name="start" type="datetime-local" required /></label>
      <label>End <input name="end" type="datetime-local" required /></label>
      <button className="primary" type="submit">Add block</button>
    </form>
  );
}
