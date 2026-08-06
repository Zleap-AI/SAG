import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./i18n/request.ts");
const configuredBasePath = process.env.NEXT_PUBLIC_APP_BASE_PATH;
const basePath = configuredBasePath && configuredBasePath !== "/"
  ? `/${configuredBasePath}`.replace(/\/+/g, "/").replace(/\/$/, "")
  : undefined;

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  basePath,
  // fnOS serves the UI through a UDS gateway prefix. Avoid Next's secondary
  // image-optimizer endpoint so public images remain ordinary prefixed assets.
  images: { unoptimized: Boolean(basePath) },
  // Keep development HMR artifacts isolated from `next build` output.
  distDir: process.env.NODE_ENV === "development" ? ".next-dev" : ".next",
  eslint: { ignoreDuringBuilds: true },
  async redirects() {
    // v0.3 客户端形态：旧路由 → 新 IA
    return [
      { source: "/overview", destination: "/chat", permanent: false },
      { source: "/assistants", destination: "/chat", permanent: false },
      { source: "/assistants/:id", destination: "/chat", permanent: false },
      { source: "/sources", destination: "/knowledge", permanent: false },
      { source: "/sources/:id", destination: "/knowledge/:id", permanent: false },
    ];
  },
};

export default withNextIntl(nextConfig);
