import localFont from "next/font/local";

// 字体随仓库自托管（latin 子集可变字体，SIL OFL 1.1，许可证见 ./fonts/）。
// 不使用 next/font/google：它会在构建期访问 fonts.googleapis.com 拉取字体，
// 在离线或受限网络（企业内网 / 部分 CI）下 `docker compose build web` 必然失败。
// 自托管后构建全程无外网依赖，字形与原先一致（下载的是同一份 latin 子集）。

// 正文与标题统一无衬线（Notion/Codex 风）；标题用紧字距 .font-display 区分
export const inter = localFont({
  src: "./fonts/inter-latin-variable.woff2",
  weight: "100 900",
  variable: "--font-inter",
  display: "swap",
});

// 代码 / 数据
export const jbmono = localFont({
  src: "./fonts/jetbrains-mono-latin-variable.woff2",
  weight: "100 800",
  variable: "--font-jbmono",
  display: "swap",
});

export const fontVars = `${inter.variable} ${jbmono.variable}`;
