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

正式安装包统一发布在 [`Zleap-AI/SAG` Releases](https://github.com/Zleap-AI/SAG/releases)：

- macOS Apple Silicon：DMG 用于安装，ZIP 与 `latest-mac.yml` 用于应用内更新；
- Windows x64：暂不签名的 NSIS EXE 用于安装，`latest.yml` 与 blockmap 用于应用内更新；Windows 可能显示“未知发布者”提示；
- `SHA256SUMS.txt` 用于校验下载完整性。

客户端后台检查更新；用户选择“下载更新”后才下载，完成后再次选择“重启并安装”才安装。普通退出、稍后处理或重启应用都不会自动安装。旧客户端通过兼容过渡版获得此能力。

## 正式发布流程

桌面版正式发布分为两个阶段：先在 PR 中准备并审核版本元数据，PR 合入 `main` 后，再从 `Zleap-AI/SAG` 的干净 `main` 分支创建并推送发布标签。请仅使用官方公开仓库，不要包含内部 Git 历史。

在版本 PR 分支上输入计划发布的下一个稳定版本号：

```bash
printf "请输入下一个稳定版本号: "
read -r VERSION
node scripts/release-public.mjs --prepare "$VERSION"
```

脚本会更新 Desktop/Web/API 版本、lockfile、README 版本徽章和 `CHANGELOG.md` 发布记录。它不会暂存或提交文件，也不会推送变更或创建标签；请按常规 PR 流程审核并合入这些元数据变更。

PR 合入后，使用已同步到最新状态的干净克隆，并输入与 PR 中准备的版本号相同的版本：

```bash
git checkout main
git pull --ff-only
printf "请输入已合入的版本号: "
read -r VERSION
make release-dry-run VERSION="$VERSION"
make release VERSION="$VERSION"
```

仅创建标签的发布脚本会检查干净的 `main` 工作区和已准备的版本元数据，然后创建注解版本标签，并将标签指向公开仓库 `origin/main` 当前提交。标签会触发 `.github/workflows/desktop-release.yml`。流水线在原生 `macos-15` ARM64 和 `windows-2025` x64 runner 上构建。版本校验通过后，两个平台构建与质量检查并行；只有完整质量检查通过、macOS 签名与公证成功，并且两个平台的更新元数据和校验文件齐全后，才会创建公开 GitHub Release。质量检查失败仍会阻止发布，但此时已经启动的构建可能继续消耗 runner 时间。

脚本不会在本地构建或上传二进制。推送标签失败时，请先排查原因再重试；已经公开的标签不可移动或复用。

### 发布耗时与缓存

`.github/workflows/desktop-dependency-cache.yml` 在 `main` 上的依赖或发布配置变化后，使用与正式构建相同的 macOS/Windows runner 预热 npm 与 uv 下载缓存。两条流水线共用 `.github/actions/setup-desktop-build/action.yml`，保持工具链和缓存键一致。预热不构建安装包、不使用签名凭据，也不发布版本。

GitHub 不允许不同标签相互读取缓存，但标签可以读取默认分支的缓存；因此只在版本标签中保存缓存，不能让下一次版本发布命中它。版本 PR 合入后，让对应的 Desktop Dependency Cache 完成再推标签，可提高命中率；缓存过期时也可在 `main` 手动运行预热。预热失败、尚未完成或缓存缺失都不阻止发布，正式构建仍执行 `npm ci` 和 `uv sync --frozen --extra desktop`，只会回到下载依赖的路径。预热会额外使用两台 runner，目标是缩短发布等待，不保证减少总 runner 用量。

Actions 将 Electron 编译、Next.js 构建、Python 冻结、资源组装分别列为步骤，便于比较耗时。macOS 的签名和公证仍由 electron-builder 管理，不跳过签名、公证、更新元数据或安装包校验。首轮优化的测量基线与验收方法见 [桌面发布性能记录](../../docs/desktop-release-performance.md)。

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

- macOS：DMG 用于安装，ZIP 用于应用内更新；
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

public 正式构建将 `SAG_UPDATE_GITHUB_REPOSITORY` 转换为 generic provider，地址为 `https://github.com/<owner>/<repo>/releases/download/desktop-manual-updates`。electron-builder 将此配置写入安装包的 `app-update.yml`。独立通道的 `latest-mac.yml` 和 `latest.yml` 只存版本、安装包的绝对下载地址、大小和 SHA512；实际安装包、blockmap 和校验文件仍在不可覆盖的 `vX.Y.Z` Release 中。

`desktop-manual-updates` 是专用的可更新元数据预发布，不是安装包版本，也不能设置为 GitHub latest。旧版 GitHub provider 继续读取最后的兼容过渡版；新客户端读取独立通道。这样离线用户跳过中间发布，也不会自动安装移除迁移能力的版本。

### 两阶段发布

1. 先发布包含手动更新、通道隔离和旧数据重建确认保护的兼容过渡版本，`apps/desktop/release-policy.json` 的 `legacyBridge` 为 `true`。发布脚本将其标记为旧通道 latest，并创建独立元数据通道与 `legacy-bridge.json` 标记。旧客户端可能自动安装此过渡版。过渡版保留既有迁移兼容能力，但 Windows 隐式重建及历史未确认重建均须重新获得用户确认，不能在启动时清理知识记录。
2. 过渡版在 Windows/macOS 验收后，再合入移除旧数据迁移的后续改动，将 `legacyBridge` 设为 `false`。后续所有正式版本均使用 `--latest=false`，只更新独立通道元数据，不移动旧通道 latest。
3. 发布脚本在缺少过渡标记、重复发布过渡版或发现旧 latest 被移动时停止。不要人工将后续版本设为 latest，也不要把两阶段改动合并成一个过渡发行包。

独立通道是唯一允许覆盖的元数据 Release，版本安装包不覆盖。元数据替换不是原子的：脚本在覆盖前备份旧元数据，失败时重试并尝试恢复，仍然失败则令流水线报错。中断期间检查更新可能暂时不可用，已安装应用不受影响。若版本发布成功但元数据上传失败，使用原构建资产重试发布脚本；它会比对已发布的 SHA256SUMS，一致才恢复元数据发布，差异则停止。不能删除或重建版本标签。如果 GitHub 留下了未完成的草稿，先检查并补齐该草稿资产与校验文件，再恢复发布；脚本不会擅自覆盖草稿。GitHub Releases 列表用于人工下载安装，`/releases/latest` 有意停留在兼容过渡版。

### 更新验收

在两个平台验证：拒绝下载时无包下载；下载完成后退出和重新启动不安装；仅点击“重启并安装”才升级；反复点击和定时检查不覆盖当前下载；旧客户端仅发现过渡版；新客户端能从独立通道发现后续版本。单元测试和类型检查不能代替安装包验收。

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
- macOS 签名与 notarization、Windows “未知发布者”安装流程，以及两个平台分别确认下载、安装的更新流程。
