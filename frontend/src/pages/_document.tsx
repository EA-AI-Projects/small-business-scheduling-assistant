import { Head, Html, Main, NextScript } from "next/document";

import { contentSecurityPolicy, parseConfig, rawConfigFromEnv } from "@/lib/config";

// Rendered once at build time. The CSP names the exact API and Cognito origins from the
// build configuration; customHttp.yml adds frame-ancestors and other response headers.
const csp = contentSecurityPolicy(parseConfig(rawConfigFromEnv()),
  process.env.NODE_ENV === "development");

export default function Document() {
  return (
    <Html lang="en">
      <Head>
        <meta httpEquiv="Content-Security-Policy" content={csp} />
        <meta name="referrer" content="no-referrer" />
        <meta name="color-scheme" content="light" />
      </Head>
      <body>
        <Main />
        <NextScript />
      </body>
    </Html>
  );
}
