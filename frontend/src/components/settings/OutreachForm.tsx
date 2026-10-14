import { useCallback, useEffect, useRef, useState } from "react";

import type { BusinessProfileBody, OutreachEditBody, OutreachState } from "@/api/types";
import { useOwner } from "@/owner/OwnerContext";
import { ApiError } from "@/lib/api";

import { BusyButton } from "../BusyButton";
import { ManualInvitationForm } from "./ManualInvitationForm";
import { SettingsAccordion } from "./SettingsAccordion";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/** The default text before the business details were saved: Settings used to store it as a custom message. */
const LEGACY_DEFAULTS = ["one week", "two weeks"].map((window) =>
  "Smart Scheduling Assistant: Would you like to book a cleaning visit in the next "
  + `${window}? Reply with a day and time that works for you, or STOP to opt out.`);

/** The default invitation, built from the saved business details; null until they are saved. */
function approvedMessage(weeks: number, profile: BusinessProfileBody | null): string | null {
  if (!profile) return null;
  const window = weeks === 1 ? "week" : "two weeks";
  return `Smart Scheduling Assistant: Would you like to book a visit for ${profile.service_name} with `
    + `${profile.display_name} in the next ${window}? Reply with a day and time that works for you, or STOP to opt out.`;
}

export function OutreachForm({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void } = {}) {
  const { data } = useOwner();
  return (
    <section className="card outreach-card">
      <h3>Booking invitations</h3>
      <p className="hint">Choose when to invite eligible clients and edit the message used by both
        weekly and immediate invitations.</p>
      <p className="hint">Only active clients with no confirmed appointment during the chosen
        window are eligible. A pending request does not count as confirmed. Each client can receive
        at most one invitation within the chosen number of weeks, measured from their last
        invitation. Changing these settings does not reset that limit.</p>
      {data.outreach && <Form state={data.outreach} zone={data.zone}
        profile={data.businessProfile?.profile ?? null} onDirtyChange={onDirtyChange} />}
    </section>
  );
}

function Form({ state, zone, profile, onDirtyChange }: { state: OutreachState; zone: string;
  profile: BusinessProfileBody | null; onDirtyChange?: (dirty: boolean) => void }) {
  const { api, notify, refresh } = useOwner();
  const [saved, setSaved] = useState(state);
  const externalVersion = useRef(state.version);
  useEffect(() => {
    if (state.version !== externalVersion.current) {
      externalVersion.current = state.version;
      setSaved(state);
    }
  }, [state]);
  const [enabled, setEnabled] = useState(state.settings.enabled);
  const [weekday, setWeekday] = useState(state.settings.weekday?.toString() ?? "");
  const [localTime, setLocalTime] = useState(state.settings.local_time?.slice(0, 5) ?? "");
  const [lookahead, setLookahead] = useState(state.settings.lookahead_weeks?.toString() ?? "");
  const storedMessage = saved.settings.message && !LEGACY_DEFAULTS.includes(saved.settings.message)
    ? saved.settings.message : null;
  const savedMessage = storedMessage ?? approvedMessage(saved.settings.lookahead_weeks ?? 1, profile) ?? "";
  const [message, setMessage] = useState(savedMessage);
  const [customized, setCustomized] = useState(
    state.settings.message != null && !LEGACY_DEFAULTS.includes(state.settings.message)
      && state.settings.message !== approvedMessage(state.settings.lookahead_weeks ?? 1, profile));
  const [openSection, setOpenSection] = useState({ schedule: true, message: false, send: false });
  const [saveError, setSaveError] = useState<string | null>(null);
  const attempt = useRef<{ signature: string; key: string } | null>(null);
  const updateMessage = useCallback((value: string) => {
    setCustomized(true);
    setMessage(value);
  }, []);
  const defaultMessage = lookahead ? approvedMessage(Number(lookahead), profile) : null;
  // Until the owner edits it, the message is the default for the chosen window and saved business details.
  const current = !customized && defaultMessage ? defaultMessage : message;
  const missing = enabled && (!weekday || !localTime || !lookahead);
  const tooLong = current.length > 500;
  // Saving the schedule never needs message text: with no saved business details and no custom
  // message it stores "no custom message". Only a send needs text, and an emptied custom one is invalid.
  const invalidSave = tooLong || (customized && !current.trim());
  const invalidSend = invalidSave || !current.trim();
  const dirty = enabled !== saved.settings.enabled
    || weekday !== (saved.settings.weekday?.toString() ?? "")
    || localTime !== (saved.settings.local_time?.slice(0, 5) ?? "")
    || lookahead !== (saved.settings.lookahead_weeks?.toString() ?? "")
    || current.trim() !== savedMessage;
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);

  function body(): OutreachEditBody {
    return {
      expected_version: saved.version,
      settings: {
        enabled,
        weekday: weekday === "" ? null : Number(weekday),
        local_time: localTime || null,
        lookahead_weeks: lookahead === "" ? null : Number(lookahead),
        // The default wording is stored as "no custom message", so it follows later changes to the business details.
        message: !current.trim() || current.trim() === defaultMessage ? null : current.trim(),
      },
    };
  }

  async function save() {
    if (missing || invalidSave) {
      setOpenSection((current) => ({ ...current, schedule: current.schedule || missing,
        message: current.message || invalidSave }));
      window.setTimeout(() => document.querySelector<HTMLElement>(missing ? '.outreach-card select' :
        '.outreach-card textarea')?.focus(), 0);
      return;
    }
    setSaveError(null);
    const sent = body();
    const signature = JSON.stringify(sent);
    if (attempt.current?.signature !== signature) attempt.current = { signature, key: crypto.randomUUID() };
    try {
      const updated = await api.request<OutreachState>("/booking-outreach", { method: "PUT", body: sent,
        idempotencyKey: attempt.current.key });
      attempt.current = null;
      setSaved(updated);
      try {
        await refresh();
        notify("Booking invitation settings saved");
      } catch (error) {
        notify(`Booking invitation settings saved, but the latest view could not load: ${
          error instanceof Error ? error.message : String(error)}`, true);
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.code === "STALE_VERSION") {
        await refresh(true).catch(() => undefined);
        setSaveError("Invitation settings changed elsewhere. Your edits are kept; review and save again.");
      } else setSaveError(error instanceof Error ? error.message : String(error));
      notify("Invitation settings were not saved", true);
    }
  }

  async function prepareSend(key: string): Promise<boolean> {
    if (missing || invalidSend || !lookahead) return false;
    if (!dirty) return true;
    try {
      const updated = await api.request<OutreachState>("/booking-outreach", { method: "PUT", body: body(),
        idempotencyKey: `${key}-settings` });
      setSaved(updated);
      try { await refresh(); } catch {
        notify("Invitation settings saved, but the latest view could not load", true);
      }
      return true;
    } catch (error) {
      notify(`Invitation settings were not saved: ${error instanceof Error ? error.message : String(error)}`, true);
      return false;
    }
  }

  return <div className="form-stack">
    <SettingsAccordion title="Recurring schedule" open={openSection.schedule}
      onToggle={() => setOpenSection((current) => ({ ...current, schedule: !current.schedule }))}>
    <label className="inline"><input type="checkbox" checked={enabled}
      onChange={(event) => setEnabled(event.target.checked)} /> Enable invitations</label>
    <div className="two-col">
      <label>Run day
        <select value={weekday} onChange={(event) => setWeekday(event.target.value)}>
          <option value="">Choose a day</option>
          {DAYS.map((day, index) => <option key={day} value={index}>{day}</option>)}
        </select>
      </label>
      <label>Run time ({zone})
        <input type="time" step="60" value={localTime}
          onChange={(event) => setLocalTime(event.target.value)} />
      </label>
    </div>
    <label>Lookahead
      <select value={lookahead} onChange={(event) => {
        setLookahead(event.target.value);
      }}>
        <option value="">Choose a window</option>
        <option value="1">One week</option>
        <option value="2">Two weeks</option>
      </select>
    </label>
    {missing && <p className="field-error" role="alert">Choose a day, time, and lookahead to enable invitations.</p>}
    </SettingsAccordion>
    <ManualInvitationForm message={current} onMessageChange={updateMessage}
      prepareSend={prepareSend} lookaheadWeeks={lookahead ? Number(lookahead) : null}
      openMessage={openSection.message} openSend={openSection.send}
      onToggleMessage={() => setOpenSection((current) => ({ ...current, message: !current.message }))}
      onToggleSend={() => setOpenSection((current) => ({ ...current, send: !current.send }))} />
    {!profile && !customized && <p className="hint">Save the business name and service under Business details to
      get the default invitation text, or write your own message.</p>}
    {invalidSave && <p className="field-error" role="alert">Enter an invitation message of up to 500 characters.</p>}
    {saveError && <p className="field-error" role="alert">{saveError}</p>}
    <div><BusyButton className="primary" onClick={save}>Save invitation settings</BusyButton></div>
    <p className="meta">Version {saved.version} · Schedule uses the business timezone above.</p>
  </div>;
}
