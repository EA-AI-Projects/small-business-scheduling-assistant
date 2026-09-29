import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { parse } from "yaml";

// Amplify reads both files from the repository root; a malformed file silently drops headers.
function repoFile(name: string): unknown {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
  return parse(readFileSync(join(root, name), "utf8"));
}

interface HeaderRule { pattern: string; headers: { key: string; value: string }[] }

describe("Amplify hosting config", () => {
  it("builds the frontend app root as a static export", () => {
    const config = repoFile("amplify.yml") as {
      applications: { appRoot: string; frontend: { artifacts: { baseDirectory: string } } }[];
    };
    expect(config.applications).toHaveLength(1);
    expect(config.applications[0]?.appRoot).toBe("frontend");
    expect(config.applications[0]?.frontend.artifacts.baseDirectory).toBe("out");
  });

  it("applies security headers to every path using the monorepo format", () => {
    const config = repoFile("customHttp.yml") as {
      applications?: { appRoot: string; customHeaders: HeaderRule[] }[]; customHeaders?: unknown;
    };
    expect(config.customHeaders).toBeUndefined();
    const app = config.applications?.find((item) => item.appRoot === "frontend");
    const all = app?.customHeaders.find((rule) => rule.pattern === "**");
    const headers = Object.fromEntries(all?.headers.map(({ key, value }) => [key, value]) ?? []);
    expect(headers["Content-Security-Policy"]).toContain("frame-ancestors 'none'");
    expect(headers["X-Frame-Options"]).toBe("DENY");
    expect(headers["X-Content-Type-Options"]).toBe("nosniff");
    expect(headers["Strict-Transport-Security"]).toContain("max-age=");
    expect(headers["Referrer-Policy"]).toBe("no-referrer");
  });
});
