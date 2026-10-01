import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { CONSENT_SCRIPT_DATE, CONSENT_SCRIPT_VERSION } from "./ConsentForm";

describe("consent script version", () => {
  it("matches the public consent page", () => {
    const page = readFileSync(resolve(__dirname, "../../../../docs/sms-consent/index.html"), "utf8");
    expect(page).toContain(`Script version ${CONSENT_SCRIPT_VERSION}, ${CONSENT_SCRIPT_DATE}`);
  });
});
