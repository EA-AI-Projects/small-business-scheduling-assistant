/**
 * Owner API types.
 *
 * Request bodies come from the generated OpenAPI schema (`schema.d.ts`, regenerate with
 * `npm run generate:api`). The owner routes return domain dataclasses without response
 * models, so response shapes are declared here from `backend/scheduling/domain`.
 */
import type { components } from "./schema";

type Schemas = components["schemas"];

export type ClientProfileBody = Schemas["ClientProfileBody"];
export type ClientNoteBody = Schemas["ClientNoteBody"];
export type LegalHoldBody = Schemas["LegalHoldBody"];
export type InPersonConsentBody = Schemas["InPersonConsentBody"];
export type DecisionBody = Schemas["DecisionBody"];
export type EditAppointmentBody = Schemas["EditAppointmentBody"];
export type BlockBody = Schemas["BlockBody"];
export type MoveBlockBody = Schemas["MoveBlockBody"];
export type RemoveBlockBody = Schemas["RemoveBlockBody"];
export type ManualAppointmentBody = Schemas["ManualAppointmentBody"];
export type PolicyEditBody = Schemas["PolicyEditBody"];
export type PolicyBody = Schemas["PolicyBody"];

export type CalendarStatus = "PENDING_APPROVAL" | "CONFIRMED" | "UNAVAILABLE" | "CANCELLED"
  | "DECLINED" | "EXPIRED";

export interface CalendarEvent {
  event_id: string;
  start_at: string;
  end_at: string;
  status: CalendarStatus;
  hold_expires_at: string | null;
  buffer_minutes: number;
  duration_minutes: number | null;
}

export interface CalendarSnapshot {
  business_id: string;
  revision: number;
  events: CalendarEvent[];
}

export interface Appointment {
  appointment_id: string;
  business_id: string;
  client_id: string;
  start_at: string;
  end_at: string;
  status: CalendarStatus;
  hold_expires_at: string | null;
  duration_minutes: number;
  buffer_minutes: number;
  version: number;
  replaces_appointment_id: string | null;
}

export interface UnavailableBlock {
  block_id: string;
  business_id: string;
  start_at: string;
  end_at: string;
  version: number;
}

export type HomeSize = "small" | "medium" | "large";

export interface ClientProfile {
  business_id: string;
  client_id: string;
  name: string;
  phone_e164: string;
  service_address: string;
  home_size: HomeSize;
  default_duration_minutes: number;
  active: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  phone_verified_at: string | null;
}

export interface ClientNote {
  business_id: string;
  client_id: string;
  note_id: string;
  appointment_id: string | null;
  body: string;
  created_by: string;
  created_at: string;
  legal_hold_reason: string | null;
}

/** A text that the carrier reported as failed or undelivered (`SmsDeliveryStatus`). */
export interface SmsDeliveryFailure {
  business_id: string;
  outbox_id: string;
  provider_id: string;
  status: string;
  recipient: string;
  observed_at: string;
  error_code: string | null;
}

export interface LocalWindow {
  opens: string;
  closes: string;
}

export interface AvailabilityPolicy {
  timezone: string;
  weekly_windows: Record<string, LocalWindow[]>;
  booking_horizon_days: number;
  slot_increment_minutes: number;
  maximum_visit_minutes: number;
  minimum_visit_gap_minutes: number;
  opening_buffer_minutes: number;
  closing_buffer_minutes: number;
  hold_minutes: number;
  maximum_buffer_minutes: number;
  date_exceptions: Record<string, LocalWindow[]>;
  holiday_calendar: Schemas["HolidayCalendar"] | null;
}

export interface PolicyState {
  record: { policy: AvailabilityPolicy; version: number };
  calendar_revision: number;
}
