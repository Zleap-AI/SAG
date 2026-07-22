# sag-api

sag 的后端服务：FastAPI + `zleap-sag`。

## 分层

| 层 | 目录 | 职责 |
|---|---|---|
| 适配层 | `sag_api/sag/` | **唯一** import `zleap-sag` 之处；信源 ↔ `DataEngine` |
| 连接器 | `sag_api/connectors/` | 采集抽象 + 注册表（文件上传 → 动态同步） |
| 文档解析 | `sag_api/parsing/` | Markdown 直通；PDF 优先 MinerU、失败自动回退；其余由 MarkItDown 转换 |
| 任务队列 | `sag_api/jobs/` | 后台处理编排（ingest → extract 状态机） |
| 生成层 | `sag_api/generation/` | 检索结果 → LLM 流式答案 + 引用 |
| 工具层 | `sag_api/tools/` | Agent 工具：内置检索/实体 + 远端 MCP 适配（统一 `Tool` 接口） |
| Agent Core | `sag_agent/` | 独立编排核心：生命周期、事件、工具、审批、取消、存储端口 |
| Agent 适配 | `sag_api/services/agent_service.py` | 将 SAG 模型、工具、会话接入 Agent Core |
| MCP | `sag_api/mcp/` | 信源即 MCP：FastMCP server + Streamable-HTTP 挂载（`/mcp/`）+ stdio 入口 |
| 领域服务 | `sag_api/services/` | 纯业务逻辑，不依赖 FastAPI |
| 接口 | `sag_api/api/v1/` | HTTP 路由，仅做 IO / 校验 / 序列化 |

## 运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn sag_api.main:app --reload --host 0.0.0.0 --port 8000
```

文档 UI：http://localhost:8000/docs

也可以在仓库根目录运行 `make api`。开发服务器默认监听全部本机网卡，便于从局域网地址访问 Web；生产环境请通过反向代理与访问控制暴露服务。
