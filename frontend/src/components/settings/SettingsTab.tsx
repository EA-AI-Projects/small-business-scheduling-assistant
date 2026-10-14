import { useCallback, useEffect, useRef, useState } from "react";

import { useOwner } from "@/owner/OwnerContext";

import { PopoverCard } from "../PopoverCard";
import { SectionHeading } from "../Workspace";
import { BusinessProfileForm } from "./BusinessProfileForm";
import { AdvancedPolicyForm } from "./AdvancedPolicyForm";
import { ExceptionForm } from "./ExceptionForm";
import { PolicySummary } from "./PolicySummary";
import { OutreachForm } from "./OutreachForm";
import { RegularPolicyForm } from "./RegularPolicyForm";

type Editor = "business" | "availability" | "advanced" | "invitations";

export function SettingsTab() {
  const { data } = useOwner();
  const [open, setOpen] = useState<Editor | null>(null);
  const [businessDirty, setBusinessDirty] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const buttons = useRef<Record<Editor, HTMLButtonElement | null>>({ business: null,
    availability: null, advanced: null, invitations: null });
  const keepEditingButton = useRef<HTMLButtonElement>(null);
  const resumeFocus = useRef<HTMLElement | null>(null);
  const getAnchor = useCallback(() => null, []);
  const policy = data.policy?.record.policy;
  const profile = data.businessProfile?.profile;
  const outreach = data.outreach?.settings;
  const openDays = policy ? Object.values(policy.weekly_windows).filter((windows) => windows.length).length : 0;
  const exceptions = policy ? Object.keys(policy.date_exceptions ?? {}).length : 0;
  const availabilityText = policy ? `${openDays} open ${openDays === 1 ? "day" : "days"} · ${
    policy.holiday_calendar === "US_FEDERAL" ? "Federal holidays closed" : "Holidays open"} · ${
    policy.booking_horizon_days}-day booking window · ${exceptions} date ${
    exceptions === 1 ? "exception" : "exceptions"}` : "Policy is not configured";
  useEffect(() => {
    if (confirmDiscard) keepEditingButton.current?.focus();
    else if (resumeFocus.current?.isConnected) {
      resumeFocus.current.focus();
      resumeFocus.current = null;
    }
  }, [confirmDiscard]);

  function show(editor: Editor) {
    setBusinessDirty(false);
    setConfirmDiscard(false);
    setOpen(editor);
  }

  function close() {
    if (confirmDiscard) { setConfirmDiscard(false); return; }
    if (open === "business" && businessDirty) {
      const active = document.activeElement;
      resumeFocus.current = active instanceof HTMLElement && active.closest(".popover-card")
        ? active : document.querySelector<HTMLElement>(".popover-close");
      setConfirmDiscard(true);
      return;
    }
    setOpen(null);
  }

  const titles: Record<Editor, string> = { business: "Business details", availability: "Availability",
    advanced: "Advanced scheduling", invitations: "Booking invitations" };

  return <>
    <SectionHeading eyebrow="AVAILABILITY" title="Settings" />
    <div className="settings-stack">
      <button ref={(node) => { buttons.current.business = node; }} type="button" className="settings-card"
        aria-haspopup="dialog" aria-label="Edit Business details" aria-describedby="settings-business-summary"
        onClick={() => show("business")}>
        <strong>Business details</strong>
        <span id="settings-business-summary">{profile ? `${profile.display_name} · ${profile.service_name} · ${
          profile.default_duration_minutes} min default visit` : "Not set up yet"}</span>
        {open === "business" && businessDirty && <span className="settings-unsaved">Unsaved changes</span>}
      </button>
      <button ref={(node) => { buttons.current.availability = node; }} type="button" className="settings-card"
        aria-haspopup="dialog" aria-label="Edit Availability" aria-describedby="settings-availability-summary"
        onClick={() => show("availability")}>
        <strong>Availability</strong>
        <span id="settings-availability-summary">{availabilityText}</span>
      </button>
      <button ref={(node) => { buttons.current.advanced = node; }} type="button" className="settings-card"
        aria-haspopup="dialog" aria-label="Edit Advanced scheduling" aria-describedby="settings-advanced-summary"
        onClick={() => show("advanced")}>
        <strong>Advanced scheduling</strong>
        <span id="settings-advanced-summary">{policy ? `${policy.slot_increment_minutes}-minute starts · ${
          policy.maximum_visit_minutes}-minute maximum visit · ${policy.timezone}` : "Set up availability first"}</span>
      </button>
      <button ref={(node) => { buttons.current.invitations = node; }} type="button" className="settings-card"
        aria-haspopup="dialog" aria-label="Edit Booking invitations" aria-describedby="settings-invitations-summary"
        onClick={() => show("invitations")}>
        <strong>Booking invitations</strong>
        <span id="settings-invitations-summary">{outreach ? outreach.enabled ? `Weekly · ${outreach.lookahead_weeks ?? "?"}-week lookahead`
          : "Recurring invitations off" : "Loading invitation settings"}</span>
      </button>
    </div>
    {open && <PopoverCard variant="modal" title={titles[open]} getAnchor={getAnchor} onClose={close}
      returnFocus={() => buttons.current[open]}>
      <div className="settings-editor">
        <div className="settings-editor-content" hidden={confirmDiscard}>
        {open === "business" && <BusinessProfileForm onDirtyChange={setBusinessDirty} />}
        {open === "availability" && <>
          <PolicySummary policy={data.policy} mode="availability" />
          {data.policy && <RegularPolicyForm />}
          {data.policy && <ExceptionForm />}
        </>}
        {open === "advanced" && (data.policy ? <AdvancedPolicyForm />
          : <p className="empty">Load the pilot policy under Availability first.</p>)}
        {open === "invitations" && <OutreachForm />}
        </div>
        {confirmDiscard && <div className="settings-discard" role="alertdialog"
          aria-label="Discard unsaved changes?">
          <p>Discard your unsaved Business details changes?</p>
          <div className="row-actions">
            <button type="button" ref={keepEditingButton} onClick={() => setConfirmDiscard(false)}>Keep editing</button>
            <button type="button" className="danger" onClick={() => { setConfirmDiscard(false); setOpen(null); }}>
              Discard changes</button>
          </div>
        </div>}
      </div>
    </PopoverCard>}
  </>;
}
