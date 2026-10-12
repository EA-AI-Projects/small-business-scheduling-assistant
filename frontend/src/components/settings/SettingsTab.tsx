import { useOwner } from "@/owner/OwnerContext";

import { SectionHeading } from "../Workspace";
import { BusinessProfileForm } from "./BusinessProfileForm";
import { AdvancedPolicyForm } from "./AdvancedPolicyForm";
import { ExceptionForm } from "./ExceptionForm";
import { PolicySummary } from "./PolicySummary";
import { OutreachForm } from "./OutreachForm";
import { RegularPolicyForm } from "./RegularPolicyForm";

export function SettingsTab() {
  const { data } = useOwner();
  return (
    <>
      <SectionHeading eyebrow="AVAILABILITY" title="Settings" />
      <div className="settings-stack">
        <BusinessProfileForm />
        <PolicySummary policy={data.policy} />
        {data.policy && <RegularPolicyForm />}
        {data.policy && <ExceptionForm />}
        {data.policy && <AdvancedPolicyForm />}
        <OutreachForm />
      </div>
    </>
  );
}
