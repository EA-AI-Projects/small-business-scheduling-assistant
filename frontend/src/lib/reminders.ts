import type { components } from "@/api/schema";

export type ReminderPreference = components["schemas"]["ReminderPreferenceView"];

export const reminderRestriction: Record<string, string> = {
  business_unconfigured: "Configure business availability and its timezone before reminders can be sent.",
  inactive_profile: "The client profile is inactive.",
  phone_unverified: "Verify the current phone before reminders can be sent.",
  consent_unavailable: "SMS consent records are unavailable; delivery eligibility cannot be verified.",
  consent_missing: "Record SMS consent for the current phone before reminders can be sent.",
  sms_opted_out: "The current phone has opted out with STOP. Reminder enrollment does not clear that restriction.",
};

/** Client-facing wording for the same blockers; display only, eligibility is decided by the server. */
export const clientReminderRestriction: Record<string, string> = {
  business_unconfigured: "The business has not finished setting up its schedule, so reminders cannot be sent yet.",
  inactive_profile: "Your profile is not active, so reminders cannot be sent. Contact the business.",
  phone_unverified: "Your phone number has not been verified by the business. Contact the business to update it.",
  consent_unavailable: "Text-message consent cannot be confirmed right now, so reminders cannot be sent.",
  consent_missing: "The business does not have your text-message consent on file. Contact the business to update it.",
  sms_opted_out: "You have opted out of texts with STOP. Turning on reminders here does not undo that.",
};

/** Client-facing explanation of one expected reminder; no delivery promise. */
export function clientReminderStatusText(
  reminder: ReminderPreference["upcoming_reminders"][number], timezone: string,
): string {
  const when = (value: string) => new Intl.DateTimeFormat("en-US", { timeZone: timezone, dateStyle: "medium", timeStyle: "short" })
    .format(new Date(value));
  const visit = `Visit ${when(reminder.start_at)}: `;
  switch (reminder.status) {
    case "disabled": return `${visit}reminders are off.`;
    case "blocked": return `${visit}a reminder cannot currently be sent.`;
    case "past_time": return `${visit}the reminder time has already passed, so no reminder will be sent for this visit.`;
    case "attempted": return `${visit}a reminder was already attempted; none will be repeated.`;
    default: return `${visit}a reminder is planned for ${when(reminder.run_at)}, but delivery is not guaranteed.`;
  }
}
