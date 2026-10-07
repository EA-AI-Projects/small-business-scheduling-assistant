import { useOwner } from "@/owner/OwnerContext";

import { SectionHeading } from "../Workspace";
import { ExceptionForm } from "./ExceptionForm";
import { PolicySummary } from "./PolicySummary";
import { OutreachForm } from "./OutreachForm";

export function SettingsTab() {
  const { data } = useOwner();
  return (
    <>
      <SectionHeading eyebrow="AVAILABILITY" title="Settings" />
      <div className="settings-stack">
        <PolicySummary policy={data.policy} />
        {data.policy && <ExceptionForm />}
        <OutreachForm />
      </div>
    </>
  );
}
