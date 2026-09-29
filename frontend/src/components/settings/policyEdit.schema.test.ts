import schema from "../../../openapi/owner.json";

import { POLICY } from "@/test/fakeApi";

import { toPolicyBody } from "./policyEdit";

describe("toPolicyBody", () => {
  it("sends exactly the fields the backend PolicyBody defines", () => {
    // If the backend adds a policy field, a date-exception save must not silently drop it.
    const expected = Object.keys(schema.components.schemas.PolicyBody.properties).sort();
    expect(Object.keys(toPolicyBody(POLICY.record.policy)).sort()).toEqual(expected);
  });
});
