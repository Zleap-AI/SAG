# fnOS 分支开发规范

本文件适用于长期分支 `fnos/develop` 及从它创建的功能 worktree。这里的规则优先于仓库通用习惯；历史文档中的旧架构和旧版本号只作为证据，不作为当前实现依据。

## 1. 分支与工作区

- `fnos/develop` 是永久独立的 fnOS 产品线，不合并回 `main`。
- 开发功能或修复时，从最新 `origin/fnos/develop` 创建独立 worktree 和 `codex/*` 功能分支；不要在主 checkout 或长期分支上直接开发。
- `main` 的通用能力只能经过评审后选择性同步到 fnOS 分支。禁止整分支 merge/rebase；不得顺带引入 Desktop、Docker、公共发布或其他与 fnOS 无关的内容。
- 同步时保留 fnOS 的无公网端口、UDS gateway、包用户权限、持久化目录和设备兼容契约，并对迁移内容运行 fnOS 完整回归。
- 开始与结束工作都要检查目标 worktree、当前分支和 `git status`，不得覆盖用户在其他 worktree 的改动。

## 2. 当前交付架构

- fnOS 只交付 Native FPK。不得重新引入 Docker、Compose、镜像仓库或宿主 TCP 服务端口。
- 包模板唯一来源是 `packages/fnos/native/sag/`；构建器位于 `scripts/build-fnos-native-*.mjs`。
- fnOS 通过 `${TRIM_APPDEST}/app.sock` 把 `/app/sag` 请求转发给 gateway；Next.js 只监听 loopback。
- Python 3.12 和 Node.js 22 由 fnOS 依赖应用提供。FPK 内只包含应用源码、目标平台二进制 wheels 和 Next standalone 产物。
- gateway、Web 和每租户 worker 都以 `sag` 包用户运行。除 fnpack 明确执行的生命周期阶段外，不依赖 root 权限。
- `${TRIM_PKGVAR}` 是 SQLite、LanceDB、上传文件、索引、日志和内部密钥的完整持久化/恢复边界；`${TRIM_PKGETC}` 不存放内部密钥。

## 3. 包契约与生命周期

- 修改 `manifest`、`config/resource`、`config/privilege`、`app/ui/config` 或 `cmd/` 时，同时修改 `scripts/validate-fnos-native-package.mjs` 和相关合约测试。
- 保持 `type=iframe`、`gatewayPrefix=/app/sag`、`gatewaySocket=app.sock`、`run-as=package`、`username/groupname=sag`、`micro_app=true`，且不得出现 `service_port` 或 `docker-project`。
- NAS 导入只声明实际使用的四项 scope：`trim.file.sharedAccess`、`trim.file.userAcl`、`trim.file.path`、`trim.system.getPlatformConfig`。
- 生命周期脚本必须兼容 POSIX `sh`（fnOS 使用 dash），使用 `set -u` 而非 `set -eu`。任何致命退出都要向 `${TRIM_TEMP_LOGFILE}` 写出可操作原因。
- 升级前执行停服冷备；普通卸载保留数据。Schema、备份、恢复、升级或卸载变更必须补充真实生命周期测试。
- 不提交 FPK、vendor、sha256 或一次性设备日志。发布资产由 fnOS Delivery 工作流生成；本地测试包只放 `/private/tmp` 或其他临时目录。

## 4. 版本与发布

- 当前 fnOS 版本格式唯一合法形式为 `x.y.z-fnos`，正则为 `^[0-9]+\.[0-9]+\.[0-9]+-fnos$`。
- 每次发布递增语义化 patch：`1.5.3-fnos` 的下一版是 `1.5.4-fnos`。不得继续生成 `1.5.3-fnos.1`、`fnos.N` 或其他构建序号。
- GitHub Release tag 为 `fnos-vx.y.z-fnos`；x86 文件名为 `sag-x.y.z-fnos-x86.fpk`。
- fnOS 版本独立于公共 `main` 版本。`scripts/fnos-version.mjs` 从现有 fnOS Release 推导下一 patch；没有 fnOS Release 时才以最新公共稳定 tag 作为初始版本。
- 迁移期间允许解析历史 `fnos-vx.y.z-fnos.N` tag，但只能把其 `x.y.z` 当作旧基线，并发布下一 patch 的新格式。
- `.github/workflows/fnos-release.yml` 只能从 `fnos/develop` 手动触发，且并发串行。禁止绕过共享版本解析器手工拼接版本。
- 本地构建也必须通过同一版本校验。测试包可指定计划版本，但不得用已发布版本覆盖现有设备包。

## 5. 身份、NAS 与安全边界

- fnOS 身份只能来自 gateway 验证结果，不能信任浏览器自报的 UID、用户名或管理员标记。所有信源、文档、任务和检索必须保持租户隔离。
- `TRIM_API_TOKEN` 每次宿主调用时重新读取，只能存在于服务端内存；禁止缓存、持久化、记录日志或返回浏览器。
- NAS 物理路径、canonical path 和 staged path 不得出现在浏览器 API。页面只使用展示路径、folder ID 和有时效且绑定 UID/source 的不透明选择 token。
- 文件扫描与复制必须限制根目录、拒绝符号链接、重新检查 ACL，并用 `lstat`、`O_NOFOLLOW`、inode/size/mtime 校验防止扫描后替换。
- NAS 导入是一次性私有复制，不是同步。未变化文件跳过；变化文件通过维护队列替换并保留文档 ID；旧派生知识在替换排队后立即停止检索。
- fnOS `1.2.0500+` 使用宿主共享目录 API；更旧版本保留应用设置手动授权目录流程。本地上传永远可用，宿主 API 失败不得封锁本地上传。

## 6. 产品与交互要求

- fnOS 专属入口仅对已验证管理员显示；非管理员不得看到目录信息或伪可用操作。
- 授权、扫描、筛选、选择、确认、进度、部分失败、过期、撤权和旧版本引导都属于功能验收范围，不是可选 UI 装饰。
- 桌面端和移动 iframe 都必须可用；固定操作区、长列表、横向表格、授权回跳、键盘和触控需要分别验证。
- 用户文案不得暴露 UDS、token、canonical path、堆栈或内部错误。用可行动的产品语言说明重试、重新授权或改用本地上传。
- 中英文 message catalog 必须保持完全同键；新增页面不能写绕过 i18n 的用户可见字符串。

## 7. 最低验证矩阵

相关修改先写失败测试，再实现。发布或交付测试包前至少执行：

```bash
# Backend
cd apps/api
uv run --extra dev ruff check sag_api sag_agent tests
uv run --extra dev pytest -q

# Frontend（顺序执行；Next build 会重建 .next/types，不能与 typecheck 并行）
cd ../web
npm ci
npm run test:unit
npm run i18n:check
npm run typecheck
npm run lint
NEXT_PUBLIC_APP_BASE_PATH=/app/sag \
  NEXT_PUBLIC_API_BASE=/app/sag \
  NEXT_PUBLIC_ENABLE_WINDOW_SCALING=0 \
  npm run build

# fnOS package / lifecycle / release contracts
cd ../..
node --test scripts/tests/fnos-*.test.mjs
```

- 包结构、权限或生命周期变更还要用真实 fnpack 构建 x86 FPK，并记录文件大小和 SHA-256。
- 单测、Unix Socket 模拟和成功构建不等于真机通过。安装、启动、停止、覆盖升级、卸载、身份、SDK 授权、旧系统 fallback、撤权、移动 iframe 和规模性能必须记录设备型号、架构、fnOS/SAG 版本、时间、截图或日志。
- 不得从本地 FPK 文件名推断线上部署版本，不得把未执行的真机项写成已验证。

## 8. 完成交付

- 用 `git diff --check`、`git status --short` 和相对 `origin/fnos/develop` 的完整 diff 核对范围、密钥、物理路径及生成物。
- 说明自动化通过项、跳过项、真机缺口、FPK 路径和 SHA-256。测试失败时报告事实并停止发布，不使用“应该可用”替代证据。
- 未经用户明确要求，不 push、不创建 Release、不合并 `fnos/develop`，也不清理用户拥有的 worktree。
