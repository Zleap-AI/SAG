# SAG CLI 使用指南

`@zleap-ai/sag-cli` 是 SAG 知识库的官方命令行客户端。从终端里你可以：

- 登录并验证一个正在运行的 SAG 实例；
- 查看信源、文档处理状态，直接检索知识内容；
- 验证本机 Docker SAG 的知识库 MCP，并把它一键接入 Codex 和 Claude Code。

## 你适不适合用它

- 你手上已经有一个 SAG 实例（本机 Docker 或远程 HTTP），想在终端里查它、调它、把它接给编码 Agent。
- 你在开发或运维 SAG，需要 `doctor`、`mcp test` 这类诊断能力。
- 你只是想在 Codex 或 Claude Code 里用 SAG 做知识检索，又不想手写 MCP 配置。

## 前置条件

| 你想做的事                  | 需要准备                                                    |
| --------------------------- | ----------------------------------------------------------- |
| 安装并运行 CLI              | Node.js **≥ 20.19**                                         |
| 用 HTTP API 登录、搜索      | 一个可访问的 SAG Origin（如 `http://localhost:8000`）+ JWT  |
| 用本机 Docker 免 Token 路径 | Docker CLI，且本机运行着含 `sag_api.mcp.server` 的 SAG 容器 |
| 接入 Codex                  | 已安装 Codex CLI（≥ 0.145）                                 |
| 接入 Claude Code            | 已安装 Claude Code（≥ 2.1）                                 |

**JWT 从哪拿**：登录 SAG Web → **Settings → Integrations**，复制其中的 JWT。CLI 会隐藏输入并优先保存到系统凭据存储（macOS Keychain、Windows Credential Manager、Linux Secret Service）；不可用时只保留在当前进程，自动化环境请改用 `SAG_TOKEN` 环境变量。

## 安装

```bash
npm install --global @zleap-ai/sag-cli
sag --help
sag version
```

## 快速上手：选一条适合你的路径

CLI 提供两条互相独立的路径，按你的场景选。

### 路径 A：本机 Docker，免 Token（推荐给本地开发者）

前提是本机 Docker 已经在跑 SAG API 容器。CLI 会自动发现容器，通过 `docker exec` 走 stdio MCP，全程不需要 JWT。

```bash
# 1. 验证本机 Docker SAG MCP 可以工作
sag mcp test

# 2. 把它接入你的 Agent（选一个或两个都接）
sag agent connect codex
sag agent connect claude-code

# 3. 随时看接入状态
sag agent status
```

只想验证 / 接入某一个信源时加 `--source-id`：

```bash
sag mcp test --source-id <source-id>
sag agent connect codex --source-id <source-id>
```

不放心可以先 `--dry-run` 看计划：

```bash
sag agent connect codex --dry-run
```

想撤掉接入：

```bash
sag agent disconnect codex
```

### 路径 B：HTTP API + JWT（跨机器、或本机没有 Docker）

用 SAG 的 HTTP API 做认证、查询、检索。

```bash
# 1. 记录一个 SAG 实例
sag profile add local http://localhost:8000
sag profile use local

# 2. 登录（会提示输入 JWT）
sag auth login
sag auth status

# 3. 体检 + 看信源
sag doctor
sag source list
sag document status --source <source-id>

# 4. 检索
sag search "MCP 如何接入" --source <source-id> --top-k 5
```

Profile URL 只保存 `scheme://host[:port]`。API 前缀 `/api/v1` 由 CLI 自动补齐，不要自己写。

## 命令一览

```text
sag version
sag auth login | status | logout
sag profile add | list | use | show | remove
sag doctor
sag source list | get | status
sag document list | get | status
sag search <query>
sag mcp test
sag agent list
sag agent connect <codex | claude-code>
sag agent status [codex | claude-code]
sag agent disconnect <codex | claude-code>
```

常用示例：

```bash
sag profile list --json
sag source get <source-id>
sag document list --source <source-id>
sag search "MCP 如何接入" --source <source-id> --strategy multi
sag mcp test --container sag-api-1 --timeout 15000
sag agent connect claude-code --name sag-knowledge-local
```

## 全局参数

```text
--profile <name>   选择 Profile
--url <origin>     临时指定 SAG Origin
--json             输出稳定 JSON（schema: sag.cli.v1）
--quiet            只输出核心值
--yes              确认安全的本地配置操作
```

## 环境变量与配置优先级

优先级：**命令行参数 > 环境变量 > 当前 Profile > 本地默认探测**。

```bash
SAG_URL=http://localhost:8000
SAG_TOKEN=<jwt>
SAG_PROFILE=local
```

Profile 配置存放位置：

| 平台    | 路径                                                                      |
| ------- | ------------------------------------------------------------------------- |
| macOS   | `~/Library/Application Support/sag-cli/config.yaml`                       |
| Linux   | `$XDG_CONFIG_HOME/sag-cli/config.yaml` 或 `~/.config/sag-cli/config.yaml` |
| Windows | `%APPDATA%\sag-cli\config.yaml`                                           |

Agent 接入的受管状态另存于 `managed-connections.yaml`（权限 `0600`），不要手工编辑。

## Agent 接入的安全约束

- SAG CLI **不会** 把 JWT 写入 Agent 配置。本机 Docker 路径无需 Token。
- 默认 MCP 名称为 `sag-knowledge-<profile>`，无 Profile 时为 `sag-knowledge-local`。
- 只删除自己创建、指纹未变化的 MCP 条目；检测到同名用户配置或外部改动会拒绝覆盖。
- `--dry-run` 只展示计划；`--yes` 跳过确认但不跳过冲突检查。

## JSON 输出与退出码

`--json` 下成功与失败使用固定 Schema：

```json
{ "schema": "sag.cli.v1", "ok": true, "data": {} }
```

```json
{
  "schema": "sag.cli.v1",
  "ok": false,
  "error": { "code": "AUTH_REQUIRED", "message": "No SAG token is configured" }
}
```

Token 不会出现在 JSON、日志或错误输出里。完整错误码与退出码见 [CLI 架构文档](https://github.com/Zleap-AI/sag-cli)。
