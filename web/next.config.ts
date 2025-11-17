import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: 'standalone', // Docker 部署优化
  // env: {
  //   NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000',
  // },
};

export default nextConfig;
