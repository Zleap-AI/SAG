# SAG Desktop

SAG Desktop 用 Electron 承载现有 Next.js 工作台，并在本机管理两个随包运行的服务：

- Next.js standalone 本地 Web 运行时；
- PyInstaller `onedir` 形式的 FastAPI/Python sidecar。

桌面版默认打开完整主面板，产品界面和路由继续来自 `apps/web`。首版不拆分宠物窗口，也不维护第二套前端。

## 本地开发

要求：

- Node.js 20+；
- Python 3.11；
- `apps/web`、`apps/api` 和 `apps/desktop` 的依赖已经安装。

首次准备：

```bash
cd apps/web
npm install

cd ../api
uv sync --extra dev --extra desktop

cd ../desktop
npm install
```

启动 Web、API 和 Electron：

```bash
cd apps/desktop
npm run dev
```

如果 3000 或 8000 端口已经运行对应服务，开发脚本会复用它们。退出 Electron 时，由脚本创建的子进程会一起退出。

## 用户下载与更新

正式安装包统一发布在 [`Zleap-AI/SAG` Releases](https://github.com/Zleap-AI/SAG/releases/latest)：

- macOS Apple Silicon：DMG 用于安装，ZIP 与 `latest-mac.yml` 用于自动更新；
- Windows x64：暂不签名的 NSIS EXE 用于安装，`latest.yml` 与 blockmap 用于自动更新；Windows 可能显示“未知发布者”提示；
- `SHA256SUMS.txt` 用于校验下载完整性。

已安装客户端默认跟随 GitHub Releases 的 `latest` 稳定通道。Release 必须是非草稿正式版本；草稿和失败的流水线不会被客户端发现。

## 一条命令发布到 public

正式发布只能从 `Zleap-AI/SAG` 的独立公开 clone 根目录、干净且已合并完成的 `main` 分支执行。该 clone 不得添加内部仓库 remote，也不得包含内部 Git 历史：

```bash
make release-dry-run VERSION=1.4.0
make release VERSION=1.4.0
```

`scripts/release-public.mjs` 会：

1. 校验当前分支、干净工作区、严格递增的稳定 SemVer，以及 fetch/push remote 是否都指向 `Zleap-AI/SAG`；
2. 拉取并确认本地 `main` 包含 `origin/main`，且两者根提交完全一致，阻止内部或其他无关历史进入公开仓库；
3. 同步 Desktop/Web/API 运行时版本及 lockfile，更新 README 徽章，将 `Unreleased` 归档为本次版本；
4. 创建 `release: vX.Y.Z` 提交和不可变注解标签；
5. 原子推送 `main + vX.Y.Z` 到公开仓库的 `origin`。任一引用推送失败时，两者都不会在远端生效。

标签随后触发 `.github/workflows/desktop-release.yml`。流水线复用完整 CI 门禁，在 `macos-15` ARM64 与 `windows-2025` x64 原生 runner 并行构建；macOS 必须签名并公证成功，Windows 明确生成无签名安装包，且两个平台更新元数据和校验文件齐全时，才创建公开 GitHub Release。

发布脚本不会在本地构建或上传二进制。推送失败时，本地发布提交与标签会保留，排查后可重试；不要移动或复用已经公开的标签。

## GitHub 发布环境

打开 public 仓库的 [`Settings → Environments`](https://github.com/Zleap-AI/SAG/settings/environments)，创建名称完全一致的 `desktop-release` Environment。若启用 Deployment branches and tags 限制，需要同时允许 `main`（手动验收）与 `v*.*.*`（正式发布标签）；可选配 Required reviewers 作为人工发布闸门。

在该 Environment 的 **Environment secrets** 中配置：

| Secret | 用途 |
| --- | --- |
| `APPLE_CERTIFICATE_BASE64` | 含私钥的 Developer ID Application `.p12` 证书单行 Base64；流水线映射为 `CSC_LINK` |
| `APPLE_CERTIFICATE_PASSWORD` | `.p12` 导出密码；流水线映射为 `CSC_KEY_PASSWORD` |
| `APPLE_ID` | Apple Developer 账号邮箱，用于公证 |
| `APPLE_APP_SPECIFIC_PASSWORD` | Apple ID 的 App 专用密码，用于公证；不是账号普通密码 |
| `APPLE_TEAM_ID` | Apple Developer Team ID |

不需要配置普通 Environment variables，也不需要自行创建 GitHub PAT；发布任务使用 GitHub 自动提供的 `GITHUB_TOKEN`，并只在最终发布 job 中申请 `contents: write`。

- 在 Apple Developer 的 Certificates, Identifiers & Profiles 中创建 **Developer ID Application** 证书，在本机钥匙串中连同私钥导出为有密码的 `.p12`；这是 `APPLE_CERTIFICATE_BASE64` 与 `APPLE_CERTIFICATE_PASSWORD` 的来源。
- 在 Apple ID 账号页创建 App 专用密码，保存为 `APPLE_APP_SPECIFIC_PASSWORD`；不要把 Apple ID 普通密码放进 GitHub。
- `APPLE_TEAM_ID` 可在 Apple Developer Membership details 中查看。

在 macOS 本机把证书转为可粘贴到 GitHub Secret 的单行 Base64：

```bash
openssl base64 -A -in DeveloperIDApplication.p12 | pbcopy
```

结果保存为 `APPLE_CERTIFICATE_BASE64`；命令只写入剪贴板，不要把结果粘贴进终端、Issue、PR 或日志。

你已有的 `APPLE_SIGNING_IDENTITY` 当前不需要接入：electron-builder 导入 `.p12` 后会自动寻找 Developer ID Application 证书。完整 identity 通常带有 `Developer ID Application:` 前缀，直接映射为 `CSC_NAME` 反而会被当前 builder 拒绝；仅当 `.p12` 含多个同类证书时，再确认无前缀的 qualifier 后显式配置。`APPLE_PASSWORD` 也不会被流水线引用，建议从 GitHub Secrets 删除；公证只使用 `APPLE_APP_SPECIFIC_PASSWORD`。

如果这些 Secrets 已经配置在仓库级的 **Settings → Secrets and variables → Actions**，引用仍然有效，无需重复创建。需要更严格隔离时，再把上表 5 个复制到 `desktop-release` Environment secrets；该 Environment 只开放给 macOS 发布 job。

Windows 暂不配置证书 Secret，流水线会关闭证书自动发现并校验安装器保持无签名。Environment 可配置 required reviewer 作为人工发布闸门。Workflow 只申请 `contents: write` 来创建 Release，macOS 签名凭据不会传给普通 CI、PR、fork 或 Windows 构建任务。

Secrets 配置完成后，可在 public 仓库的 **Actions → Desktop Release → Run workflow** 中选择 `main` 做一次手动验收。它会运行完整质量门禁、macOS 签名与公证、Windows 无签名构建，并保留 7 天临时 Artifacts；手动运行不会创建 GitHub Release。只有推送注解标签 `vX.Y.Z` 才进入公开发布步骤。

## 本地构建与排查

发布构建必须在目标操作系统上执行。PyInstaller sidecar 含操作系统和 CPU 架构相关的原生库，不能在 macOS 上生成可发布的 Windows sidecar。

macOS Apple Silicon：

```bash
cd apps/desktop
npm run dist:mac
```

Windows x64：

```powershell
cd apps/desktop
npm run dist:win
```

构建顺序固定为：

1. 编译 Electron main/preload；
2. 用桌面 API 地址重新构建 Next.js standalone；
3. 冻结 Python sidecar；
4. 组装 Web、API 和运行清单；
5. 生成安装产物；macOS 额外执行签名和公证，Windows 当前保持无签名。

产物位于 `apps/desktop/release/`：

- macOS：DMG 用于安装，ZIP 用于自动更新；
- Windows：NSIS 安装器及其更新元数据。

只验证应用目录、不生成安装器时，可运行：

```bash
npm run package:dir
```

## 构建与发布配置

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SAG_DESKTOP_APP_ID` | `ai.zleap.sag` | 应用唯一标识；首次公开发布后不得随意修改 |
| `SAG_DESKTOP_API_PORT` | `8000` | 本地 API 端口；同时写入 Web 构建和桌面运行时 |
| `SAG_DESKTOP_WEB_PORT` | `32100` | 本地 Web 首选端口；被占用时向后寻找可用端口 |
| `SAG_UPDATE_GITHUB_REPOSITORY` | 未设置 | GitHub 更新源，格式 `owner/repository`；正式流水线传入 `Zleap-AI/SAG` |
| `SAG_UPDATE_BASE_URL` | 未设置 | 备用通用更新源根地址；不能与 GitHub 更新源同时设置 |
| `SAG_NOTARIZE` | `false` | 设为 `true` 时执行 macOS notarization |
| `SAG_DESKTOP_PYTHON` | `apps/api/.venv` 中的 Python | 构建 sidecar 使用的解释器 |
| `SAG_PYTHON_DIST_DIR` | API 默认冻结产物目录 | 复用 CI 中已构建的 sidecar |

macOS 签名凭据只注入 electron-builder 的最终签名与公证步骤，不会传给 Next.js、PyInstaller 或它们的构建依赖，也不写入仓库。Windows 当前不注入签名凭据。应用图标母版和平台产物位于 `apps/desktop/assets/icon-master.png`、`icon.icns` 与 `icon.ico`。

`SAG_DESKTOP_API_PORT` 属于发布构建参数，不建议交给最终用户修改，因为 Next.js 中的 API Base 是构建时值。若确实修改，构建和运行阶段必须保持一致。

## 运行与数据目录

正式客户端只监听 loopback：

- Web：`localhost:32100` 起的动态端口；
- API/MCP：`127.0.0.1:8000`。

数据库、上传文件、知识引擎数据和桌面运行密钥不写入安装目录，而是写入 Electron 标准 `userData` 目录：

- macOS：`~/Library/Application Support/SAG/`
- Windows：`%APPDATA%\SAG\`

应用更新不会覆盖此目录；Windows 卸载器也配置为默认保留用户数据。

## 更新约束

桌面版采用整包版本和整包更新：Electron、Next.js、Python API 及其原生依赖使用同一个 `apps/desktop/package.json` 版本发布。不要分别更新 Web 或 Python sidecar，否则无法保证接口和数据迁移兼容。

public 正式构建使用 GitHub provider，并把安装包、ZIP/EXE 更新载荷、blockmap、`latest-mac.yml` 与 `latest.yml` 发布在同一个非草稿 Release。electron-builder 在安装包内生成 `app-update.yml`，客户端据此发现后续稳定版本；不要把更新地址固定到某个版本标签的下载目录。

备用自托管场景可以设置 `SAG_UPDATE_BASE_URL` 使用 generic provider，但必须自行保证同一稳定 URL 始终提供最新元数据和对应载荷。未配置 provider 的开发/本地产物不会生成更新配置，也不会检查更新。

## 发布前检查

至少完成：

```bash
npm run typecheck
npm run prepare:release
```

并在干净的目标机器验证：

- 首次安装和首次冷启动；
- Web 登录页与 `/api/v1/system/ready`；
- 文档导入、搜索、对话、探索模式和 MCP；
- 应用退出后两个本地服务均结束；
- 覆盖升级保留用户数据；
- macOS 签名与 notarization、Windows “未知发布者”安装流程，以及两个平台的自动更新。
