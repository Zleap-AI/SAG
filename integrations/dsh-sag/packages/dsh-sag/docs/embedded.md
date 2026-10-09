# dsh-sag embedded 模式

embedded 模式面向需要由 DeepSeek Harness 0.2.0-rc.2 直接启动 `zleap-sag==0.14.0` 引擎的高级部署。普通本机 SAG 用户不需要此模式。插件与 Python 侧车均要求精确的引擎版本；其他版本会在初始化前失败。

## 准备运行时

需要 Python 3.11+ 和 `uv`。先按插件 README 将当前源码构建的包安装到目标 profile；新版本公开发布前，请使用本地打包结果。

在 SAG 仓库的插件目录中可以直接准备运行时：

```sh
cd integrations/dsh-sag
node scripts/setup-runtime.mjs --python python3 --target "$HOME/.local/share/dsh-sag"
```

`dsh plugin ... exec` 会在该 profile 的安装目录中运行命令，因此不依赖当前目录存在 `node_modules`：

```sh
dsh plugin --profile web exec node node_modules/@zleap-ai/dsh-sag/scripts/setup-runtime.mjs --python python3 --target "$HOME/.local/share/dsh-sag"
```

命令会在目标目录的 `dsh-sag-runtime-0.14.0` 中创建固定版本的 Python 环境，并输出解释器路径。旧 `dsh-sag-runtime-0.1.0` 目录继续保留，升级不会重装旧侧车使用的解释器。

## 配置

准备 SAG Engine 环境文件，例如 `/etc/dsh-sag/sag.env`。SQLite 和 LanceDB 默认存储在侧车工作目录的 `./.zleap/`；建议显式设置 `SAG_DATA_DIR`，重启与升级均使用相同路径。`SAG_STORAGE_MODE` 必须是 `normal` 或 `lite`；`fast` 使用 chunk 向量检索，因此需要 `normal`。

```dotenv
SAG_STORAGE_MODE=normal
SAG_DATA_DIR=/var/lib/dsh-sag
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://your-provider/v1
LLM_MODEL=your-llm-model
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_SCHEMA_DIMENSIONS=1024
EMBEDDING_REQUEST_DIMENSIONS=none
```

上例适用于返回固定 1024 维向量、请求不接受 `dimensions` 参数的模型。请按实际模型调整维度与端点。Embedding 没有单独设置 `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` 时沿用 `OPENAI_*`。

从 0.10.0 升级时，删除环境文件及侧车进程环境中的旧 `EMBEDDING_DIMENSIONS`：即使其值为空，0.14.0 也会拒绝启动并提示新配置名称。`EMBEDDING_SCHEMA_DIMENSIONS` 决定向量库 schema 与返回向量校验；`EMBEDDING_REQUEST_DIMENSIONS` 只控制请求中的 `dimensions` 参数，两项独立。需要保留旧版显式 `EMBEDDING_DIMENSIONS=N` 的组合行为时，将两项都设为 `N`；固定维度模型通常只设置 schema，并将 request 设为 `none` 或省略。旧版未配置维度的部署可继续省略两项，两项默认均为未设置。不要在已有存储上更改模型或向量维度。

以下三个环境变量分别表示 Python 解释器、SAG 环境文件和允许使用的 namespace：

```sh
export DSH_SAG_PYTHON="$HOME/.local/share/dsh-sag/dsh-sag-runtime-0.14.0/bin/python"
export DSH_SAG_ENV_FILE=/etc/dsh-sag/sag.env
export DSH_SAG_NAMESPACES='[{"id":"product-docs","label":"产品文档"}]'
```

Harness home 优先使用 `$DSH_HOME`，未设置时是 `~/.dsh`。将下列 patch 写入 `$DSH_HOME/profiles/web/cordis.patch.yml`；如果未设置 `$DSH_HOME`，对应路径是 `~/.dsh/profiles/web/cordis.patch.yml`：

```yaml
- id: dsh-sag
  name: '@zleap-ai/dsh-sag'
  config:
    mode: embedded
    pythonCommand: !!js process.env.DSH_SAG_PYTHON
    envFile: !!js process.env.DSH_SAG_ENV_FILE
    namespaces: !!js JSON.parse(process.env.DSH_SAG_NAMESPACES)
    defaultMode: fast
    maxResults: 20
    maxReadEngines: 4
    requestTimeoutMs: 30000
    shutdownGraceMs: 5000
```

每个 namespace 的 `id` 必须与 SAG 数据源一致，不能为空、不能重复，最长 36 个 URL-safe 字符。`label` 只用于向用户和模型说明其内容。

## 启动与诊断

插件加载时会检查 Python 可执行文件、环境文件、协议版本、`zleap-sag` 版本、引擎健康状态、证据读取能力和 namespace 列表。任一检查失败都会停止加载，并在 dsh 日志中给出具体原因。

先在不启动服务的情况下检查最终配置，再启动 `web` profile 并观察启动日志：

```sh
dsh --profile web --dump-config
dsh web
```

卸载插件或停止 dsh 时，插件先向 sidecar 发送 `shutdown` 并等待引擎释放资源；超过 `shutdownGraceMs` 后会终止受管进程树。运行中检索超时由 `requestTimeoutMs` 控制。
