import type { NextConfig } from "next";

// Static export only: no SSR, server actions, or API routes. Amplify serves `out/`.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  reactStrictMode: true,
  poweredByHeader: false,
  images: { unoptimized: true },
  // Do not let `next dev` write AGENTS.md/CLAUDE.md into the repository.
  agentRules: false,
};

export default nextConfig;
