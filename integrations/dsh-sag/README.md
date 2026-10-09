# dsh-sag

dsh-sag 是 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的 SAG 知识库插件，让 dsh 检索和阅读知识、上传文件、写入笔记并管理文档。

源码在 SAG 仓库中统一维护，npm 包仍为 `@zleap-ai/dsh-sag`，使用独立版本和发布流程。原始导入基线为 dsh-sag `0.1.1` / `15b1363a24b18394dfcd85172fa68d66030b9482`。

## 安装和连接

本源码版本为 `0.2.0`，适配 DeepSeek Harness `0.2.0-rc.2`。默认连接已运行的 SAG，不需要 Python；高级 embedded 模式使用 `zleap-sag==0.14.0`。

先启动 SAG，准备 Node.js 24、pnpm 11.7.0 和 dsh 0.2.0-rc.2。如果尚未安装 dsh，可以运行 `npm install --global @deepseek-ai/dsh@0.2.0-rc.2`。

用户通过 npm 安装插件。以下命令在 `0.2.0` 发布到 npm 后可用。

```sh
dsh plugin --profile web add @zleap-ai/dsh-sag@0.2.0
dsh plugin --profile web exec dsh-sag doctor
dsh --profile web
```

`doctor` 显示“SAG 已连接”后，直接在 dsh 中提出任务，例如“在 SAG 里查找上传限制，并引用原文”。如果未发现 SAG，可自动发现、使用 SAG 导出的连接文件，或指定本机地址：

```sh
dsh plugin --profile web exec dsh-sag setup
dsh plugin --profile web exec dsh-sag setup ./sag-dsh.json
dsh plugin --profile web exec dsh-sag setup --url http://127.0.0.1:8000
```

使用导出文件时，请在文件所在目录执行命令，或将 `./sag-dsh.json` 替换为实际路径。

插件能力与使用指南：[中文](docs/usage.zh.md) · [English](docs/usage.md)。包安装指南：[中文](packages/dsh-sag/README.zh.md) · [English](packages/dsh-sag/README.md) · [embedded 模式](packages/dsh-sag/docs/embedded.md)。

## 工程结构

- `packages/dsh-sag/src/`：dsh 工具、SAG 连接、命令行和 sidecar 协议。
- `python/runtime/`：embedded Python 运行时的唯一编辑源。
- `scripts/`：CLI 打包、运行时同步、安装验收和发布验证。
- `packages/dsh-sag/runtime/`：由 `scripts/sync-runtime.mjs` 从 canonical Python 源生成的 npm 交付副本。
- `examples/`：隔离测试示例。

默认 local 模式通过 SAG REST 执行操作，通过 MCP 握手检查检索能力。embedded 模式直接托管 Python 引擎，只提供检索和阅读。

## 开发与源码安装

开发或调试插件修改时，在本目录构建并安装本地包。

```sh
pnpm install --frozen-lockfile
pnpm run pack
dsh plugin --profile web add ./artifacts/zleap-ai-dsh-sag-0.2.0.tgz
```

随后运行 `dsh plugin --profile web exec dsh-sag doctor`，再启动或重启对应 profile。

## 开发验证

```sh
pnpm run lint
pnpm run typecheck
pnpm run test
pnpm run test:python
pnpm run test:release
pnpm run check:pack
```

`check:pack` 使用 `DSH_BIN` 指定的宿主（默认 PATH 中的 dsh），验证干净 profile 的真实 tarball 安装、CLI、Web 起停、错误配置拒绝和 Cordis 共享。Python 测试验证 installed 0.14.0 引擎及 stdio 生命周期；模型服务使用隔离的确定性响应，不能代表外部模型质量。

在 SAG 完整源码中，还可以运行真实后端验收：

```sh
uv sync --project ../../apps/api --frozen --extra dev
pnpm run build
node scripts/run-live-sag.mjs
```

脚本使用临时目录和动态本机端口，启动真实 SAG API 与已发布的 0.14.0 引擎，验证全部 11 个插件工具、上传处理、检索阅读及 API 重启后的连接恢复，结束时停止所创建的服务。它输出证据目录；外部模型由本机确定性响应代替。

插件发布使用 `dsh-sag-v<版本>` 标签，与 SAG 的 `v<版本>` 桌面发布分开。见[独立发布说明](docs/releasing.md)。
