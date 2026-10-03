import { useEffect, useRef, type ReactNode } from "react";

import { rangeTitle, rangeTitleShort, todayKey } from "@/lib/time";
import { useOwner, type Tab } from "@/owner/OwnerContext";

import { ClientsTab } from "./clients/ClientsTab";
import { RequestsTab } from "./requests/RequestsTab";
import { ScheduleTab } from "./schedule/ScheduleTab";
import { SettingsTab } from "./settings/SettingsTab";
import { AppHeader } from "./shell/AppHeader";
import { CalendarControls } from "./shell/CalendarControls";

const SECTIONS: { id: Tab; label: string }[] = [
  { id: "schedule", label: "Schedule" },
  { id: "requests", label: "Requests" },
  { id: "clients", label: "Clients" },
  { id: "settings", label: "Settings" },
];

export function Workspace({ onSignOut, notice }: { onSignOut: () => void; notice?: ReactNode }) {
  const { tab, setTab, notify, data, loaded, date, view, setView, goToday, goToDate, stepRange } = useOwner();
  const pending = data.requests.length;
  // A section opened from deep in another (e.g. "Open client" below the calendar) starts at its top.
  // The previous section's result message is cleared too.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    notify("");
    window.scrollTo?.(0, 0);
  }, [tab, notify]);
  const current = SECTIONS.find((item) => item.id === tab);
  return (
    <div>
      <AppHeader appName="Scheduling" current={tab} badgeTotal={pending}
        items={SECTIONS.map((item) => ({ ...item, badge: item.id === "requests" ? pending : undefined }))}
        onSelect={(id) => setTab(id as Tab)}
        menuFooter={<>
          <span className="meta">Owner signed in</span>
          <button type="button" onClick={onSignOut}>Sign out</button>
        </>}>
        {tab === "schedule" ? (
          <CalendarControls title={loaded ? rangeTitle(date, view) : "Loading…"}
            shortTitle={loaded ? rangeTitleShort(date, view) : undefined} view={view} disabled={!loaded} onToday={goToday}
            date={date} today={loaded ? todayKey(data.zone) : ""} onPick={goToDate} onStep={stepRange} onView={setView} />
        ) : (
          <span className="range-title">{current?.label}</span>
        )}
      </AppHeader>
      {notice}
      <section className="tab-panel" aria-label="Schedule" hidden={tab !== "schedule"}><ScheduleTab /></section>
      <section className="tab-panel" aria-label="Requests" hidden={tab !== "requests"}><RequestsTab /></section>
      <section className="tab-panel" aria-label="Clients" hidden={tab !== "clients"}><ClientsTab /></section>
      <section className="tab-panel" aria-label="Settings" hidden={tab !== "settings"}><SettingsTab /></section>
    </div>
  );
}

export function SectionHeading({ eyebrow, title, children }: {
  eyebrow: string; title: string; children?: React.ReactNode;
}) {
  return (
    <div className="section-heading">
      {/* tabIndex -1 lets code move focus here when a section is opened from another one. */}
      <div><span className="eyebrow">{eyebrow}</span><h2 tabIndex={-1}>{title}</h2></div>
      {children}
    </div>
  );
}
