# 自托管字体

`app/fonts.ts` 通过 `next/font/local` 加载本目录的字体，**构建期不访问任何外部网络**。

| 文件 | 字体 | 子集 | 字重 | 许可证 |
| --- | --- | --- | --- | --- |
| `inter-latin-variable.woff2` | Inter | latin | 100–900（可变） | SIL OFL 1.1（`LICENSE-Inter.txt`） |
| `jetbrains-mono-latin-variable.woff2` | JetBrains Mono | latin | 100–800（可变） | SIL OFL 1.1（`LICENSE-JetBrainsMono.txt`） |

来源：Google Fonts CSS API（`fonts.googleapis.com/css2?family=Inter:wght@100..900`、`...family=JetBrains+Mono:wght@100..800`）返回的 latin 子集 woff2，与原先 `next/font/google` 在构建期下载的文件完全一致，因此**视觉无变化**。

## 维护约定

- 升级：重新请求上述 CSS 拿到新的 woff2 覆盖同名文件即可。
- **不要**改回 `next/font/google`：那会让 Docker 构建再次依赖 Google Fonts 的可达性（见 issue #168）。
- 中文（CJK）不在 latin 子集内，仍由 `app/globals.css` 的 `--font-sans` / `--font-mono` 回落到 PingFang SC 等系统字体，与改动前行为一致。
