import { ApiError, OwnerApi, path } from "./api";
import { parseConfig } from "./config";

const config = parseConfig({ authMode: "local", apiBaseUrl: "http://127.0.0.1:8000", businessId: "pilot" });

function respond(status: number, body: unknown): typeof fetch {
  return vi.fn(async () => new Response(JSON.stringify(body), { status }));
}

describe("OwnerApi", () => {
  it("sends the bearer token only to the business-scoped API path", async () => {
    const fetcher = respond(200, { ok: true });
    await new OwnerApi(config, "tok", () => undefined, fetcher).get("/calendar");
    const [url, init] = vi.mocked(fetcher).mock.calls[0]!;
    expect(url).toBe("http://127.0.0.1:8000/v1/owner/businesses/pilot/calendar");
    expect(init?.headers).toEqual({ Authorization: "Bearer tok" });
    expect(init?.credentials).toBe("omit");
  });

  it("adds JSON and a fresh idempotency key to commands", async () => {
    const fetcher = respond(200, {});
    const api = new OwnerApi(config, "tok", () => undefined, fetcher);
    await api.request("/blocks", { method: "POST", body: { a: 1 }, idempotent: true });
    await api.request("/blocks", { method: "POST", body: { a: 1 }, idempotent: true });
    const [first, second] = vi.mocked(fetcher).mock.calls.map(([, init]) => init?.headers as Record<string, string>);
    expect(first?.["Content-Type"]).toBe("application/json");
    expect(first?.["Idempotency-Key"]).toBeTruthy();
    expect(first?.["Idempotency-Key"]).not.toBe(second?.["Idempotency-Key"]);
  });

  it("surfaces the API error and conflict state, and signs out on 401", async () => {
    const onUnauthorized = vi.fn();
    const conflict = new OwnerApi(config, "tok", onUnauthorized,
      respond(409, { error: { code: "STALE_VERSION", message: "Stale" }, current: { version: 3 } }));
    await expect(conflict.get("/x")).rejects.toMatchObject({ status: 409, code: "STALE_VERSION",
      message: "Stale", current: { version: 3 } });
    const expired = new OwnerApi(config, "tok", onUnauthorized, respond(401, {}));
    await expect(expired.get("/x")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});

describe("path", () => {
  it("encodes interpolated identifiers", () => {
    expect(path`/clients/${"a/b?c"}/notes`).toBe("/clients/a%2Fb%3Fc/notes");
  });
});
