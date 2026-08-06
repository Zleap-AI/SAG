"""错误分类三维度：错误码（code）+ 链路环节（stage）+ 责任归属（layer）。

背景：历史上错误码只有 HTTP 语义（not_found / upstream_error / …），
一个 `UpstreamError` 同时装了「引擎内部错」「LLM 厂商错」「DB 存储错」，
用户上报日志时无法判断问题出在哪一环、该找谁排查。

这里定义描述一个错误的三个正交维度，是全项目错误分类的唯一事实来源：

- **code（错误码）**：机器可读的错误身份，前端据此做逻辑分支、错误文案映射。
  历史上散落在各抛出点的字面量（``code="xxx"``）统一收敛到 :class:`ErrorCode`，
  杜绝魔法值；新增错误码一律在本文件登记。
- **layer（责任归属）**：这个错误的根子在谁那里 —— 前端 / SAG 自身 /
  zleap-sag 引擎 / LLM 厂商 / 存储。决定「找谁排查」。
- **stage（链路环节）**：错误发生在业务链路的哪一步。决定「哪一步崩了」。

前端把 code/layer/stage/message/request_id 一并采集进诊断日志，研发拿到
日志即可精准定位「哪个环节 + 谁的责任 + 具体报错原文」。

维护约定：
- 枚举**成员值**即对外契约（前端逻辑、诊断日志、SSE 帧都依赖它），
  一旦发布**不可随意更名**；确需废弃时保留旧值并标注 deprecated。
- 新增错误码时挑选或新增合适的分组，补一行 docstring 说明触发场景。
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """全项目机器可读错误码的唯一登记处。

    成员值必须与历史字面量逐字一致（前后端契约），只做集中收敛、不改语义。
    按业务域分组，新增时归入对应分组或新建分组。
    """

    # —— 通用 HTTP 语义（ApiError 家族基类默认码）——
    INTERNAL_ERROR = "internal_error"
    """未归类的服务端内部错误（500 兜底）。"""

    NOT_FOUND = "not_found"
    """请求的资源不存在（404）。"""

    CONFLICT = "conflict"
    """资源冲突，如重复创建（409）。"""

    VALIDATION_ERROR = "validation_error"
    """输入参数校验失败（422）。"""

    UNAUTHORIZED = "unauthorized"
    """未认证或凭证无效（401）。"""

    FORBIDDEN = "forbidden"
    """已认证但无权访问该资源（403）。"""

    CONFIGURATION_ERROR = "configuration_error"
    """缺少必要配置，如未配置 LLM（400）。"""

    UPSTREAM_ERROR = "upstream_error"
    """上游（LLM / 引擎）返回错误（502）。"""

    SERVICE_UNAVAILABLE = "service_unavailable"
    """暂时不可用，可重试，如限流 / 超时（503）。"""

    # —— LLM / 结构化输出 ——
    LLM_UNAVAILABLE = "llm_unavailable"
    """LLM 暂时性失败：超时 / 限流 / 5xx，可重试。"""

    LLM_AUTH_ERROR = "llm_auth_error"
    """LLM 鉴权失败：API Key 或权限问题，需改配置，不可重试。"""

    LLM_BAD_REQUEST = "llm_bad_request"
    """LLM 拒绝请求：请求非法 / 上下文超限。"""

    LLM_EMPTY_RESPONSE = "llm_empty_response"
    """LLM 未返回任何候选答案。"""

    SCHEMA_VALIDATION_ERROR = "schema_validation_error"
    """模型输出不符合结构化 schema（如 references 的 minItems）。"""

    # —— 分页 / 游标 ——
    INVALID_CURSOR = "invalid_cursor"
    """消息分页游标无效（签名不符 / 格式错误 / 过长）。"""

    INVALID_PAGE_LIMIT = "invalid_page_limit"
    """分页大小越界。"""

    # —— 知识宇宙（universe）——
    SNAPSHOT_CHANGED = "snapshot_changed"
    """探索期间知识图谱快照已变更，需重新开始当前探索。"""

    # —— 检索 / 信源 ——
    TOO_MANY_SEARCH_SOURCES = "too_many_search_sources"
    """单次检索指定的信源数量超过上限。"""

    # —— 流式传输（SSE）——
    STREAM_ERROR = "stream_error"
    """SSE 流式生成中途意外中断（问答 / 搜索通用）。"""

    # —— MCP 工具 ——
    MCP_CONNECTION_FAILED = "mcp_connection_failed"
    """连接外部 MCP 服务器失败。"""


class ErrorLayer(StrEnum):
    """责任归属：这个错误应该找谁排查。"""

    CLIENT = "client"
    """前端 / 网络 / SSE 协议层 —— 浏览器侧或链路传输问题。"""

    API = "api"
    """SAG 后端自身 —— 编排、鉴权、参数校验、配置缺失等本地逻辑。"""

    ENGINE = "engine"
    """zleap-sag 引擎 —— 分块、抽取、schema 校验、引擎内部存储等。"""

    LLM = "llm"
    """LLM 厂商 —— 超时、限流、鉴权失败、返回结构不合规（如 schema 拒绝）。"""

    STORE = "store"
    """持久化层 —— 数据库 / 向量库读写、事务、外键约束等。"""


class ErrorStage(StrEnum):
    """链路环节：错误发生在业务流程的哪一步。

    文档摄入链：upload → parse → chunk → embed → extract → persist
    问答链：      retrieve → generate → tool → persist
    横切：        config / auth / unknown
    """

    # —— 文档摄入链 ——
    UPLOAD = "upload"
    """上传接收：文件校验（扩展名 / 大小 / 空文件）、落盘。"""

    PARSE = "parse"
    """解析：非 Markdown 文档转 Markdown（MinerU / MarkItDown）。"""

    CHUNK = "chunk"
    """分块：文本切片，写入 chunk 与其向量前的加载阶段。"""

    EMBED = "embed"
    """向量化：调用 embedding 模型生成向量。"""

    EXTRACT = "extract"
    """提取事项：逐 chunk 抽取事件 / 实体（structured output）。"""

    PERSIST = "persist"
    """入库：chunk / 事件 / 答案的持久化与计数提交。"""

    # —— 问答链 ——
    RETRIEVE = "retrieve"
    """召回：向量 / 多路检索相关片段。"""

    GENERATE = "generate"
    """生成：LLM 生成答案（含流式 turn）。"""

    TOOL = "tool"
    """工具调用：Agent 执行 search_context / web_search 等工具。"""

    # —— 横切 ——
    CONFIG = "config"
    """配置：LLM / embedding / 引擎所需配置缺失或非法。"""

    AUTH = "auth"
    """鉴权：未认证、凭证失效、无权访问。"""

    UNKNOWN = "unknown"
    """未归类：尚未打上 stage 标记的错误。"""
