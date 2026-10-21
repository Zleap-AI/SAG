import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  // Packaged Desktop resources are signed and must stay immutable at runtime.
  // The image optimizer writes to distDir/cache/images; serve bundled images directly.
  images: { unoptimized: process.env.SAG_DESKTOP_BUILD === "true" },
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
