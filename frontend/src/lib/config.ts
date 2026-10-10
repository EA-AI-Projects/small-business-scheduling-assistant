/**
 * Public, build-time owner app configuration. Every value here ships in the static
 * bundle, so it must never contain a secret, owner subject, or customer data.
 */

export type AuthMode = "cognito" | "local";

export interface OwnerConfig {
  authMode: AuthMode;
  /** Owner API origin (and optional base path), without a trailing slash. */
  apiBaseUrl: string;
  businessId: string;
  /** Cognito hosted UI domain, e.g. https://prefix.auth.us-west-1.amazoncognito.com */
  cognitoDomain: string | null;
  /** Public Cognito app client ID (no secret). */
  clientId: string | null;
}

export interface RawConfig {
  authMode?: string | undefined;
  apiBaseUrl?: string | undefined;
  businessId?: string | undefined;
  cognitoDomain?: string | undefined;
  clientId?: string | undefined;
}

export class ConfigError extends Error {}

const BUSINESS_ID = /^[A-Za-z0-9_-]+$/;
const LOOPBACK = new Set(["localhost", "127.0.0.1"]);

function parseUrl(name: string, value: string | undefined, allowLoopbackHttp: boolean): string {
  if (!value) throw new ConfigError(`${name} is required`);
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new ConfigError(`${name} must be an absolute URL`);
  }
  const loopbackHttp = allowLoopbackHttp && url.protocol === "http:" && LOOPBACK.has(url.hostname);
  if (url.protocol !== "https:" && !loopbackHttp) throw new ConfigError(`${name} must use HTTPS`);
  if (url.username || url.password || url.search || url.hash) {
    throw new ConfigError(`${name} must not contain credentials, a query, or a fragment`);
  }
  if (value.endsWith("/")) throw new ConfigError(`${name} must not end with a slash`);
  return value;
}

export function parseConfig(raw: RawConfig): OwnerConfig {
  const authMode = raw.authMode || "cognito";
  if (authMode !== "cognito" && authMode !== "local") {
    throw new ConfigError("NEXT_PUBLIC_AUTH_MODE must be cognito or local");
  }
  const local = authMode === "local";
  const apiBaseUrl = parseUrl("NEXT_PUBLIC_API_BASE_URL", raw.apiBaseUrl, local);
  if (local && !LOOPBACK.has(new URL(apiBaseUrl).hostname)) {
    throw new ConfigError("NEXT_PUBLIC_API_BASE_URL must be a loopback address (127.0.0.1 or localhost) in local mode");
  }
  if (!raw.businessId || !BUSINESS_ID.test(raw.businessId)) {
    throw new ConfigError("NEXT_PUBLIC_BUSINESS_ID must be letters, digits, _ or -");
  }
  if (local) {
    return { authMode, apiBaseUrl, businessId: raw.businessId, cognitoDomain: null, clientId: null };
  }
  const cognitoDomain = parseUrl("NEXT_PUBLIC_COGNITO_DOMAIN", raw.cognitoDomain, false);
  if (new URL(cognitoDomain).pathname !== "/") {
    throw new ConfigError("NEXT_PUBLIC_COGNITO_DOMAIN must be an origin with no path");
  }
  if (!raw.clientId) throw new ConfigError("NEXT_PUBLIC_COGNITO_CLIENT_ID is required");
  return { authMode, apiBaseUrl, businessId: raw.businessId, cognitoDomain, clientId: raw.clientId };
}

/** Next.js inlines NEXT_PUBLIC_* only for literal property reads, so list each one. */
export function rawConfigFromEnv(): RawConfig {
  return {
    authMode: process.env.NEXT_PUBLIC_AUTH_MODE,
    apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL,
    businessId: process.env.NEXT_PUBLIC_BUSINESS_ID,
    cognitoDomain: process.env.NEXT_PUBLIC_COGNITO_DOMAIN,
    clientId: process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID,
  };
}

/** Origins the page may contact; used to build the Content-Security-Policy. */
export function connectOrigins(config: OwnerConfig): string[] {
  const origins = [new URL(config.apiBaseUrl).origin];
  if (config.cognitoDomain) origins.push(new URL(config.cognitoDomain).origin);
  return origins;
}

export function contentSecurityPolicy(config: OwnerConfig, development = false): string {
  // Next's dev server needs eval and inline styles for fast refresh; production does not.
  const script = development ? "'self' 'unsafe-eval'" : "'self'";
  const style = development ? "'self' 'unsafe-inline'" : "'self'";
  const connect = ["'self'", ...connectOrigins(config), ...(development ? ["ws:"] : [])];
  return [
    "default-src 'none'",
    `script-src ${script}`,
    `style-src ${style}`,
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src ${connect.join(" ")}`,
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; ");
}
