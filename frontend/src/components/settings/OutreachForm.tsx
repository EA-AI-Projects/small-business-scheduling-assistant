import { useState } from "react";

import type { OutreachEditBody, OutreachState } from "@/api/types";
import { useOwner } from "@/owner/OwnerContext";

import { BusyButton } from "../BusyButton";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function OutreachForm() {
  const { data } = useOwner();
  return (
    <section className="card outreach-card">
      <h3>Booking invitations</h3>
      <p className="hint">Choose when to invite eligible clients to request a cleaning visit.
        Saving these settings does not start text delivery; outreach remains unavailable until
        consent, campaign, sender, and live SMS requirements are approved.</p>
      <p className="hint">Only active clients with no confirmed appointment during the chosen
        window are eligible. A pending request does not count as confirmed. Each client can receive
        at most one invitation within the chosen number of weeks, measured from their last
        invitation. Changing these settings does not reset that limit.</p>
      {data.outreach && <Form key={data.outreach.version} state={data.outreach} zone={data.zone} />}
    </section>
  );
}

function Form({ state, zone }: { state: OutreachState; zone: string }) {
  const { change } = useOwner();
  const [enabled, setEnabled] = useState(state.settings.enabled);
  const [weekday, setWeekday] = useState(state.settings.weekday?.toString() ?? "");
  const [localTime, setLocalTime] = useState(state.settings.local_time?.slice(0, 5) ?? "");
  const [lookahead, setLookahead] = useState(state.settings.lookahead_weeks?.toString() ?? "");
  const missing = enabled && (!weekday || !localTime || !lookahead);

  async function save() {
    if (missing) return;
    const body: OutreachEditBody = {
      expected_version: state.version,
      settings: {
        enabled,
        weekday: weekday === "" ? null : Number(weekday),
        local_time: localTime || null,
        lookahead_weeks: lookahead === "" ? null : Number(lookahead),
      },
    };
    await change("/booking-outreach", "PUT", body, "Booking invitation settings saved");
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
      <select value={lookahead} onChange={(event) => setLookahead(event.target.value)}>
        <option value="">Choose a window</option>
        <option value="1">One week</option>
        <option value="2">Two weeks</option>
      </select>
    </label>
    {missing && <p className="field-error" role="alert">Choose a day, time, and lookahead to enable invitations.</p>}
    <div><BusyButton className="primary" onClick={save}>Save invitation settings</BusyButton></div>
    <p className="meta">Version {state.version} · Schedule uses the business timezone above.</p>
  </div>;
}
