import type { OwnerConfig } from "./config";

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code: string | null,
    readonly current: unknown) {
    super(message);
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Send a fresh Idempotency-Key; required by scheduling commands and note creation. */
  idempotent?: boolean;
  /** Reuse a command key when retrying after an uncertain response. */
  idempotencyKey?: string;
  /** Require an exact success status when the endpoint's completion contract demands one. */
  expectedStatus?: number;
}

export type Fetcher = typeof fetch;

/** Authenticated client for one business. The token is held only by this object. */
export class OwnerApi {
  private readonly base: string;
  readonly businessId: string;

  constructor(config: OwnerConfig, private readonly token: string,
    private readonly onUnauthorized: () => void,
    // Wrap the global so it is never invoked as a method of this object ("Illegal invocation").
    private readonly fetcher: Fetcher = (input, init) => fetch(input, init)) {
    this.base = `${config.apiBaseUrl}/v1/owner/businesses/${encodeURIComponent(config.businessId)}`;
    this.businessId = config.businessId;
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.token}` };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
    else if (options.idempotent) headers["Idempotency-Key"] = crypto.randomUUID();
    let response: Response;
    try {
      response = await this.fetcher(`${this.base}${path}`, {
        method: options.method ?? "GET", headers, credentials: "omit", cache: "no-store",
        referrerPolicy: "no-referrer",
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
    } catch {
      throw new ApiError("The owner API could not be reached", 0, null, undefined);
    }
    const data: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      if (response.status === 401) this.onUnauthorized();
      const error = (data as { error?: { code?: string; message?: string } } | null)?.error;
      throw new ApiError(error?.message || `Server returned ${response.status}`, response.status,
        error?.code ?? null, (data as { current?: unknown } | null)?.current);
    }
    if (options.expectedStatus !== undefined && response.status !== options.expectedStatus) {
      throw new ApiError(`Server returned ${response.status}; expected ${options.expectedStatus} to confirm completion`,
        response.status, null, undefined);
    }
    return data as T;
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>(path);
  }
}

export function path(template: TemplateStringsArray, ...values: string[]): string {
  return template.reduce((out, part, index) =>
    out + part + (index < values.length ? encodeURIComponent(values[index] ?? "") : ""), "");
}
