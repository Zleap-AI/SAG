import localFont from "next/font/local";

// 正文与标题统一无衬线（Notion/Codex 风）；标题用紧字距 .font-display 区分
export const inter = localFont({
  src: "./fonts/Inter-Variable.woff2",
  weight: "100 900",
  style: "normal",
  variable: "--font-inter",
  display: "swap",
});

// 代码 / 数据
export const jbmono = localFont({
  src: "./fonts/JetBrainsMono-Variable.woff2",
  weight: "100 800",
  style: "normal",
  variable: "--font-jbmono",
  display: "swap",
});

export const fontVars = `${inter.variable} ${jbmono.variable}`;
