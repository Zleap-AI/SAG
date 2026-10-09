# 使用 dsh-sag

[English](usage.md) · [集成概览](../README.md)

`@zleap-ai/dsh-sag` 让 DeepSeek Harness 检索和阅读 SAG 知识库，上传文件、保存文本，并管理知识库与文档。源码在 SAG 仓库中维护，npm 包使用独立版本和发布流程。

默认 `local` 模式连接已经运行的 SAG 应用，通过 SAG REST 执行操作，并通过 MCP 检查检索能力。dsh 无需准备 Python 运行时。

## 兼容版本

当前 dsh-sag **0.2.0** 支持 DeepSeek Harness **0.2.0-rc.2**。

准备 Node.js 24 和 pnpm 11.7.0，确保 `dsh` 与 `pnpm` 在 PATH 中。SAG 需要包含 **设置 → 集成 → 连接 dsh** 和本地连接器 API。

## 安装并启动

先启动 SAG。安装当前支持的 dsh 宿主时，执行以下命令。

```sh
npm install --global @deepseek-ai/dsh@0.2.0-rc.2
```

用户通过 npm 安装插件。以下命令在 0.2.0 发布到 npm 后可用。

```sh
dsh plugin --profile web add @zleap-ai/dsh-sag@0.2.0
dsh plugin --profile web exec dsh-sag doctor
dsh --profile web
```

`doctor` 显示 `SAG 已连接` 后，即可在 dsh 对话中使用 SAG。本机通常会自动连接，无需手工填写密钥。

## 保存连接

如果自动发现没有成功，任选一种方式配置连接。

```sh
# 自动发现已经启动的本机 SAG
dsh plugin --profile web exec dsh-sag setup

# 使用 SAG“连接 dsh”设置导出的文件
dsh plugin --profile web exec dsh-sag setup ./sag-dsh.json

# 指定 SAG 的本机地址
dsh plugin --profile web exec dsh-sag setup --url http://127.0.0.1:8000
```

使用导出文件时，请在文件所在目录执行命令，或将 `./sag-dsh.json` 替换为实际路径。配置后运行 `dsh plugin --profile web exec dsh-sag doctor`，再启动或重启 dsh。

所有步骤使用同一 profile 和相同的 `DSH_HOME`。自定义 profile 请一致替换所有命令中的 `web`。

## local 模式工具

插件注册以下 11 个工具。知识库与文档操作调用接口前，都会检查当前 SAG 声明的对应能力。

| 工具 | 用途 |
| --- | --- |
| `sag_status` | 检查 SAG 连接并报告可用能力。 |
| `sag_list_sources` | 列出知识库及其稳定 ID。 |
| `sag_create_source` | 创建可接收文件或文本的知识库。 |
| `sag_search` | 检索知识库并返回证据引用。 |
| `sag_read` | 使用检索返回的 `evidence_ref` 分页阅读证据命中的原文片段。 |
| `sag_list_documents` | 列出知识库中的文档和处理状态。 |
| `sag_get_document` | 查看单个文档及其当前处理状态。 |
| `sag_reprocess_document` | 提交文档重新处理任务并返回已接受的任务状态。 |
| `sag_delete_document` | 经 dsh 用户确认后删除文档。 |
| `sag_upload_file` | 上传一个 SAG 允许的本机文件并返回处理状态。 |
| `sag_ingest_text` | 将文本保存为文档并返回处理状态。 |

文件上传、文本写入和重新处理均为异步操作。通过文档查看工具检查进度，状态变为 `ready` 后再检索。SAG 声明允许的文件类型，文件大小需同时满足 SAG 和插件的限制。

上传文件、写入文本或管理文档时，若存在多个知识库且未配置默认项，请明确指定目标知识库。先使用 `sag_search`，再将返回的 `evidence_ref` 原样交给 `sag_read`，后续分页使用返回的下一页偏移量。

## 直接告诉 dsh

- “在 SAG 中查找上传限制，并给出原文依据。”
- “把 `./product-manual.pdf` 上传到产品文档知识库，处理就绪后总结内容。”
- “把下面的会议结论作为笔记保存到 SAG。”
- “列出产品文档知识库中的文档，重新处理失败的文档。”

dsh 会使用已注册的工具执行这些任务。删除文档时会请求用户确认。

## embedded 模式

高级部署可以让 dsh 管理 Python 侧车，运行精确版本 `zleap-sag==0.14.0`。该模式只为已配置的 namespace 注册 `sag_search` 和 `sag_read`。

参阅 [embedded 指南](../packages/dsh-sag/docs/embedded.md)，准备 Python 3.11+、uv、引擎环境和 namespace 配置。

## 开发与源码安装

开发或调试插件修改时，在 SAG 源码仓库根目录构建本地包。

```sh
cd integrations/dsh-sag
pnpm install --frozen-lockfile
pnpm run pack
dsh plugin --profile web add ./artifacts/zleap-ai-dsh-sag-0.2.0.tgz
```

随后运行 `dsh plugin --profile web exec dsh-sag doctor`，再启动或重启对应 profile。

升级与恢复详见[包用户指南](../packages/dsh-sag/README.zh.md)，验证和 npm 发布详见[独立发布说明](releasing.md)。
