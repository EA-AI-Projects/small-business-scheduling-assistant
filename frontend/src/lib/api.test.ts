import { OwnerApi } from "./api";
import { parseConfig } from "./config";

const config = parseConfig({ authMode: "local", apiBaseUrl: "http://127.0.0.1:8000", businessId: "pilot" });

function respond(): typeof fetch {
  return vi.fn(async () => new Response("{}", { status: 200 }));
}

describe("OwnerApi HTTP boundary", () => {
  it("sends authenticated requests to the scoped endpoint without browser credentials", async () => {
    const fetcher = respond();
    await new OwnerApi(config, "tok", () => undefined, fetcher).get("/calendar");
    const [url, init] = vi.mocked(fetcher).mock.calls[0]!;
    expect(url).toBe("http://127.0.0.1:8000/v1/owner/businesses/pilot/calendar");
    expect(init?.headers).toEqual({ Authorization: "Bearer tok" });
    expect(init?.credentials).toBe("omit");
  });

  it("sends JSON with a fresh idempotency key for each command", async () => {
    const fetcher = respond();
    const api = new OwnerApi(config, "tok", () => undefined, fetcher);
    await api.request("/blocks", { method: "POST", body: { a: 1 }, idempotent: true });
    await api.request("/blocks", { method: "POST", body: { a: 1 }, idempotent: true });
    const calls = vi.mocked(fetcher).mock.calls;
    const first = calls[0]?.[1];
    const firstHeaders = first?.headers as Record<string, string>;
    const secondHeaders = calls[1]?.[1]?.headers as Record<string, string>;
    expect(first?.body).toBe(JSON.stringify({ a: 1 }));
    expect(firstHeaders["Content-Type"]).toBe("application/json");
    expect(firstHeaders["Idempotency-Key"]).toBeTruthy();
    expect(firstHeaders["Idempotency-Key"]).not.toBe(secondHeaders["Idempotency-Key"]);
  });

  it("calls the browser fetch without binding it to the API object", async () => {
    vi.stubGlobal("fetch", function strictFetch(this: unknown) {
      if (this !== undefined && this !== globalThis) throw new TypeError("Illegal invocation");
      return Promise.resolve(new Response("{}", { status: 200 }));
    });
    try {
      await expect(new OwnerApi(config, "tok", () => undefined).get("/calendar")).resolves.toEqual({});
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
