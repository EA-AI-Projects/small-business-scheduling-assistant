import { useOwner, type Tab } from "@/owner/OwnerContext";

import { ClientsTab } from "./clients/ClientsTab";
import { RequestsTab } from "./requests/RequestsTab";
import { ScheduleTab } from "./schedule/ScheduleTab";
import { SettingsTab } from "./settings/SettingsTab";

const TABS: { id: Tab; label: string }[] = [
  { id: "schedule", label: "Schedule" },
  { id: "requests", label: "Requests" },
  { id: "clients", label: "Clients" },
  { id: "settings", label: "Settings" },
];

export function Workspace() {
  const { tab, setTab, data } = useOwner();
  return (
    <div>
      <nav className="tabs" aria-label="Owner workspace" role="tablist">
        {TABS.map((item) => (
          <button key={item.id} type="button" role="tab" aria-selected={tab === item.id}
            className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}>
            {item.label}
            {item.id === "requests" && data.requests.length > 0 ? ` (${data.requests.length})` : ""}
          </button>
        ))}
      </nav>
      <section className="tab-panel" role="tabpanel" hidden={tab !== "schedule"}><ScheduleTab /></section>
      <section className="tab-panel" role="tabpanel" hidden={tab !== "requests"}><RequestsTab /></section>
      <section className="tab-panel" role="tabpanel" hidden={tab !== "clients"}><ClientsTab /></section>
      <section className="tab-panel" role="tabpanel" hidden={tab !== "settings"}><SettingsTab /></section>
    </div>
  );
}

export function SectionHeading({ eyebrow, title, children }: {
  eyebrow: string; title: string; children?: React.ReactNode;
}) {
  return (
    <div className="section-heading">
      <div><span className="eyebrow">{eyebrow}</span><h2>{title}</h2></div>
      {children}
    </div>
  );
}
