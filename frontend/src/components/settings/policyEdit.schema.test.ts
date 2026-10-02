import schema from "../../../openapi/owner.json";

import type { AvailabilityPolicy } from "@/api/types";

import { toPolicyBody } from "./policyEdit";

describe("toPolicyBody", () => {
  it("sends exactly the fields the backend PolicyBody defines", () => {
    // If the backend adds a policy field, a date-exception save must not silently drop it.
    const expected = Object.keys(schema.components.schemas.PolicyBody.properties).sort();
    const policy: AvailabilityPolicy = {
      timezone: "America/Los_Angeles",
      weekly_windows: {},
      booking_horizon_days: 14,
      slot_increment_minutes: 15,
      maximum_visit_minutes: 180,
      minimum_visit_gap_minutes: 30,
      opening_buffer_minutes: 0,
      closing_buffer_minutes: 0,
      hold_minutes: 1440,
      maximum_buffer_minutes: 30,
      date_exceptions: {},
      holiday_calendar: "US_FEDERAL",
    };
    expect(Object.keys(toPolicyBody(policy)).sort()).toEqual(expected);
  });
});
