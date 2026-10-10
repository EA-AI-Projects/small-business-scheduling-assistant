import { ConfigError, parseConfig } from "@/lib/config";

const local = (apiBaseUrl: string) => () => parseConfig({ authMode: "local", apiBaseUrl, businessId: "pilot" });

describe("local mode API base URL", () => {
  it("accepts only loopback hosts", () => {
    expect(local("http://127.0.0.1:8000")().apiBaseUrl).toBe("http://127.0.0.1:8000");
    expect(local("http://localhost:8000")().authMode).toBe("local");
  });
  it.each(["https://api.example.test", "http://192.168.1.10:8000", "http://example.test:8000"])(
    "refuses %s", (url) => { expect(local(url)).toThrow(ConfigError); });
  it("still lets cognito mode use an HTTPS API", () => {
    expect(parseConfig({ authMode: "cognito", apiBaseUrl: "https://api.example.test", businessId: "pilot",
      cognitoDomain: "https://auth.example.test", clientId: "public" }).authMode).toBe("cognito");
  });
});
