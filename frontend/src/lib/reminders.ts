export interface ReminderPreference {
  enabled: boolean;
  version: number;
  blockers: string[];
}

export const reminderRestriction: Record<string, string> = {
  inactive_profile: "The client profile is inactive.",
  phone_unverified: "Verify the current phone before reminders can be sent.",
  consent_unavailable: "SMS consent records are unavailable; delivery eligibility cannot be verified.",
  consent_missing: "Record SMS consent for the current phone before reminders can be sent.",
  sms_opted_out: "The current phone has opted out with STOP. Reminder enrollment does not clear that restriction.",
};
