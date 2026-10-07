import { useCallback, useState } from "react";

import type { OutreachEditBody, OutreachState } from "@/api/types";
import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";
import { ManualInvitationForm } from "./ManualInvitationForm";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function approvedMessage(weeks: number): string {
  const window = weeks === 1 ? "one week" : "two weeks";
  return "Smart Scheduling Assistant: Would you like to book a cleaning visit in the next "
    + `${window}? Reply with a day and time that works for you, or STOP to opt out.`;
}

export function OutreachForm() {
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
      {data.outreach && <Form key={data.outreach.version} state={data.outreach} zone={data.zone} />}
    </section>
  );
}

function Form({ state, zone }: { state: OutreachState; zone: string }) {
  const { api, change, notify } = useOwner();
  const [saved, setSaved] = useState(state);
  const [enabled, setEnabled] = useState(state.settings.enabled);
  const [weekday, setWeekday] = useState(state.settings.weekday?.toString() ?? "");
  const [localTime, setLocalTime] = useState(state.settings.local_time?.slice(0, 5) ?? "");
  const [lookahead, setLookahead] = useState(state.settings.lookahead_weeks?.toString() ?? "");
  const savedMessage = saved.settings.message ?? approvedMessage(saved.settings.lookahead_weeks ?? 1);
  const [message, setMessage] = useState(savedMessage);
  const [customized, setCustomized] = useState(
    state.settings.message != null && state.settings.message !==
      approvedMessage(state.settings.lookahead_weeks ?? 1));
  const updateMessage = useCallback((value: string) => {
    setCustomized(true);
    setMessage(value);
  }, []);
  const missing = enabled && (!weekday || !localTime || !lookahead);
  const invalidMessage = !message.trim() || message.length > 500;
  const dirty = enabled !== saved.settings.enabled
    || weekday !== (saved.settings.weekday?.toString() ?? "")
    || localTime !== (saved.settings.local_time?.slice(0, 5) ?? "")
    || lookahead !== (saved.settings.lookahead_weeks?.toString() ?? "")
    || message.trim() !== savedMessage;

  function body(): OutreachEditBody {
    return {
      expected_version: saved.version,
      settings: {
        enabled,
        weekday: weekday === "" ? null : Number(weekday),
        local_time: localTime || null,
        lookahead_weeks: lookahead === "" ? null : Number(lookahead),
        message: message.trim(),
      },
    };
  }

  async function save() {
    if (missing || invalidMessage) return;
    await change("/booking-outreach", "PUT", body(), "Booking invitation settings saved");
  }

  async function prepareSend(key: string): Promise<boolean> {
    if (missing || invalidMessage || !lookahead) return false;
    if (!dirty) return true;
    try {
      const updated = await api.request<OutreachState>("/booking-outreach", { method: "PUT", body: body(),
        idempotencyKey: `${key}-settings` });
      setSaved(updated);
      return true;
    } catch (error) {
      notify(`Invitation settings were not saved: ${error instanceof Error ? error.message : String(error)}`, true);
      return false;
    }
  }

  return <div className="form-stack">
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
        if (!customized && event.target.value) setMessage(approvedMessage(Number(event.target.value)));
      }}>
        <option value="">Choose a window</option>
        <option value="1">One week</option>
        <option value="2">Two weeks</option>
      </select>
    </label>
    <ManualInvitationForm message={message} onMessageChange={updateMessage}
      prepareSend={prepareSend} lookaheadWeeks={lookahead ? Number(lookahead) : null} />
    {missing && <p className="field-error" role="alert">Choose a day, time, and lookahead to enable invitations.</p>}
    {invalidMessage && <p className="field-error" role="alert">Enter an invitation message of up to 500 characters.</p>}
    <div><BusyButton className="primary" onClick={save}>Save invitation settings</BusyButton></div>
    <p className="meta">Version {saved.version} · Schedule uses the business timezone above.</p>
  </div>;
}
