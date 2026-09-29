import type { NextConfig } from "next";

// Static export only: no SSR, server actions, or API routes. Amplify serves `out/`.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  reactStrictMode: true,
  poweredByHeader: false,
  images: { unoptimized: true },
};

export default nextConfig;
