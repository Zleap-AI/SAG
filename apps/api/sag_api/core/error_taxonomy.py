"""错误分类维度：链路环节（stage）+ 责任归属（layer）。

背景：历史上错误码只有 HTTP 语义（not_found / upstream_error / …），
一个 `UpstreamError` 同时装了「引擎内部错」「LLM 厂商错」「DB 存储错」，
用户上报日志时无法判断问题出在哪一环、该找谁排查。

这里引入两个正交维度，与现有 HTTP `code` 并存（不替换、向后兼容）：

- **layer（责任归属）**：这个错误的根子在谁那里 —— 前端 / SAG 自身 /
  zleap-sag 引擎 / LLM 厂商 / 存储。决定「找谁排查」。
- **stage（链路环节）**：错误发生在业务链路的哪一步。决定「哪一步崩了」。

前端把 layer/stage/code/message/request_id 一并采集进诊断日志，研发拿到
日志即可精准定位「哪个环节 + 谁的责任 + 具体报错原文」。
"""

from __future__ import annotations

from enum import StrEnum


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
