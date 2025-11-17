/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  // 不设置默认值，让 api-client.ts 中的逻辑来处理
  // 本地开发：从 .env.local 读取
  // Docker环境：不设置，使用空字符串走Nginx代理
}

module.exports = nextConfig

