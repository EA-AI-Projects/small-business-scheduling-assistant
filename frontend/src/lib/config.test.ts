import { ConfigError, contentSecurityPolicy, parseConfig } from "./config";

const COGNITO = {
  authMode: "cognito",
  apiBaseUrl: "https://abc123.execute-api.us-west-1.amazonaws.com",
  businessId: "pilot",
  cognitoDomain: "https://owner-pilot.auth.us-west-1.amazoncognito.com",
  clientId: "public-client",
};

describe("parseConfig", () => {
  it("accepts a complete Cognito configuration", () => {
    expect(parseConfig(COGNITO)).toEqual({ ...COGNITO });
  });

  it("defaults to Cognito mode and requires its settings", () => {
    expect(() => parseConfig({ ...COGNITO, authMode: undefined, clientId: undefined }))
      .toThrow("NEXT_PUBLIC_COGNITO_CLIENT_ID");
  });

  it.each([
    ["http://abc.example.com", "HTTPS"],
    ["https://abc.example.com/", "slash"],
    ["https://abc.example.com?x=1", "query"],
    ["not a url", "absolute"],
  ])("rejects API base URL %s", (apiBaseUrl, message) => {
    expect(() => parseConfig({ ...COGNITO, apiBaseUrl })).toThrow(message);
  });

  it("rejects a Cognito domain with a path", () => {
    expect(() => parseConfig({ ...COGNITO, cognitoDomain: "https://x.example.com/login" }))
      .toThrow(ConfigError);
  });

  it("rejects unsafe business IDs and unknown modes", () => {
    expect(() => parseConfig({ ...COGNITO, businessId: "../other" })).toThrow("BUSINESS_ID");
    expect(() => parseConfig({ ...COGNITO, authMode: "none" })).toThrow("AUTH_MODE");
  });

  it("allows loopback HTTP only in local mode", () => {
    const local = { authMode: "local", apiBaseUrl: "http://127.0.0.1:8000", businessId: "pilot" };
    expect(parseConfig(local)).toMatchObject({ authMode: "local", cognitoDomain: null, clientId: null });
    expect(() => parseConfig({ ...local, apiBaseUrl: "http://10.0.0.5:8000" })).toThrow("HTTPS");
    expect(() => parseConfig({ ...COGNITO, apiBaseUrl: "http://127.0.0.1:8000" })).toThrow("HTTPS");
  });
});

describe("contentSecurityPolicy", () => {
  it("allows only self scripts and the configured API and Cognito origins", () => {
    const csp = contentSecurityPolicy(parseConfig(COGNITO));
    expect(csp).toContain("script-src 'self';");
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain(
      "connect-src 'self' https://abc123.execute-api.us-west-1.amazonaws.com https://owner-pilot.auth.us-west-1.amazoncognito.com");
    expect(csp).not.toContain("unsafe");
  });
});
