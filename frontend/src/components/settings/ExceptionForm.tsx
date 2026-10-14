import { useEffect, useRef, useState, type FormEvent } from "react";
import type { LocalWindow } from "@/api/types";

import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";
import { shortTime, withDateException, type ExceptionMode } from "./policyEdit";
import { FailureAlert } from "./FailureAlert";
import { usePolicySave } from "./usePolicySave";
import { SettingsAccordion } from "./SettingsAccordion";

export function ExceptionForm({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void } = {}) {
  const { data } = useOwner();
  const { save, failure, clearFailure } = usePolicySave("Date exception saved");
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [extraWindows, setExtraWindows] = useState<LocalWindow[]>([]);
  const invalidRef = useRef<"date" | "closes" | null>(null);

  const formRef = useRef<HTMLFormElement>(null);
  const snapshot = (form: HTMLFormElement) => JSON.stringify([...new FormData(form).entries()]);
  const baseline = useRef<string | null>(null);
  const pendingBaseline = useRef(false);
  useEffect(() => {
    if (!formRef.current) return;
    if (baseline.current === null || pendingBaseline.current) baseline.current = snapshot(formRef.current);
    pendingBaseline.current = false;
  }, [extraWindows]);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!invalidRef.current) return;
    (formRef.current?.elements.namedItem(invalidRef.current) as HTMLElement | null)?.focus();
    invalidRef.current = null;
  }, [error, open]);

  function select(date: string, windows: { opens: string; closes: string }[]) {
    const form = formRef.current;
    if (!form) return;
    if (dirty && !window.confirm("Discard unsaved date exception changes?")) return;
    (form.elements.namedItem("date") as HTMLInputElement).value = date;
    (form.elements.namedItem("mode") as HTMLSelectElement).value = windows.length ? "custom" : "closed";
    (form.elements.namedItem("opens") as HTMLInputElement).value = shortTime(windows[0]?.opens ?? "08:00");
    (form.elements.namedItem("closes") as HTMLInputElement).value = shortTime(windows[0]?.closes ?? "17:00");
    pendingBaseline.current = true;
    setExtraWindows(windows.slice(1).map(({ opens, closes }) => ({ opens: shortTime(opens), closes: shortTime(closes) })));
    setDirty(false);
    setError(null);
    setOpen(true);
    (form.elements.namedItem("date") as HTMLInputElement).focus();
  }

  async function submit(form: HTMLFormElement) {
    setError(null);
    clearFailure();
    const values = new FormData(form);
    const date = String(values.get("date"));
    const mode = String(values.get("mode")) as ExceptionMode;
    const window = { opens: String(values.get("opens")), closes: String(values.get("closes")) };
    const windows = [window, ...extraWindows];
    if (!date) { invalidRef.current = "date"; setOpen(true); setError("Choose a date."); return; }
    if (mode === "custom" && windows.some((period) =>
      !period.opens || !period.closes || period.closes <= period.opens)) {
      setError("Closing time must be after opening time.");
      setOpen(true);
      invalidRef.current = "closes";
      return;
    }
    // A failed save leaves the form as typed; a committed save clears it unless the owner typed more meanwhile.
    const sent = snapshot(form);
    if (await save((policy) => withDateException(policy, date, mode, windows)) && snapshot(form) === sent) {
      form.reset();
      pendingBaseline.current = true;
      setExtraWindows([]);
      setDirty(false);
    }
  }

  const exceptions = Object.entries(data.policy?.record.policy.date_exceptions ?? {})
    .sort(([a], [b]) => a.localeCompare(b));
  return (
    <section className="card">
      <SettingsAccordion title="Date exceptions" open={open} onToggle={() => setOpen((current) => !current)}>
      <p className="hint">Close one date or set custom hours, including opening a holiday. Existing appointments
        may block the change.</p>
      {exceptions.length ? <div className="exception-list" aria-label="Saved date exceptions">
        {exceptions.map(([date, windows]) => <button key={date} type="button"
          onClick={() => select(date, windows)}>{date}: {windows.length ? windows.map((item) =>
            `${shortTime(item.opens)}–${shortTime(item.closes)}`).join(", ") : "Closed"}</button>)}
      </div> : <p className="empty">No date exceptions</p>}
      <form ref={formRef} className="form-stack" aria-label="Date exception"
        onInput={(event) => setDirty(snapshot(event.currentTarget) !== (baseline.current ?? "[]"))}
        onChange={(event) => setDirty(snapshot(event.currentTarget) !== (baseline.current ?? "[]"))}
        onSubmit={(event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void submit(event.currentTarget); }}>
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
        {extraWindows.map((period, index) => <div className="two-col" key={index}>
          <label>Open period {index + 2}<input name={`opens-${index + 2}`} type="time" value={period.opens}
            onChange={(event) => { setExtraWindows((current) => current.map((item, i) =>
              i === index ? { ...item, opens: event.target.value } : item)); }} /></label>
          <label>Close period {index + 2}<input name={`closes-${index + 2}`} type="time" value={period.closes}
            onChange={(event) => { setExtraWindows((current) => current.map((item, i) =>
              i === index ? { ...item, closes: event.target.value } : item)); }} /></label>
        </div>)}
        {error && <p id="date-exception-error" className="field-error" role="alert">{error}</p>}
        <FailureAlert failure={failure} />
        <BusyButton className="primary" onClick={() => submit(formRef.current!)}>Save exception</BusyButton>
      </form>
      </SettingsAccordion>
    </section>
  );
}
