from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from sag_api.enums import SearchStrategy
from sag_api.schemas.insight import EntityOut, GraphRelationOut


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    strategy: SearchStrategy | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)


class GlobalSearchRequest(BaseModel):
    """工作空间级搜索：默认有界候选分区，可传 source_ids 收窄（如 @某信源）。"""

    query: str = Field(min_length=1, max_length=4000)
    source_ids: list[str] | None = Field(default=None, max_length=256)
    top_k: int | None = Field(default=None, ge=1, le=50)
    strategy: SearchStrategy | None = None
    save_exploration: bool = False


class SectionOut(BaseModel):
    chunk_id: str | None
    heading: str
    content: str
    score: float
    rank: int
    source_id: str | None
    source_name: str | None = None


class SearchEventOut(BaseModel):
    id: str
    document_id: str | None = None
    source_id: str | None = None
    source_name: str | None = None
    title: str
    summary: str = ""
    category: str = ""
    rank: int = 0
    parent_id: str | None = None
    chunk_id: str | None = None
    start_time: datetime | None = None
    score: float = 0.0


class SearchSourceHitOut(BaseModel):
    source_id: str
    source_name: str | None = None
    event_hits: int = 0
    max_score: float = 0.0
    latest_event_time: datetime | None = None


class SearchResponse(BaseModel):
    query: str
    sections: list[SectionOut]
    events: list[SearchEventOut] = Field(default_factory=list)
    entities: list[EntityOut] = Field(default_factory=list)
    relations: list[GraphRelationOut] = Field(default_factory=list)
    source_hits: list[SearchSourceHitOut] = Field(default_factory=list)
    summary: str = ""
    exploration_id: str | None = None
    stats: dict[str, Any]


class EvalCompareRequest(BaseModel):
    """/search/eval-compare 请求体:同一 query,多策略并排跑。"""

    query: str = Field(min_length=1, max_length=4000)
    strategies: list[SearchStrategy] = Field(min_length=2, max_length=4)
    source_ids: list[str] | None = Field(default=None, max_length=256)
    top_k: int | None = Field(default=None, ge=1, le=50)
    judge: bool = True  # 前端可临时关掉;是否真的调 LLM 还看后台 settings 开关


class EvalStrategyResultOut(BaseModel):
    strategy: SearchStrategy
    sections: list[SectionOut]
    stats: dict[str, Any]
    error: str | None = None


class EvalJudgeOut(BaseModel):
    a_strategy: SearchStrategy
    b_strategy: SearchStrategy
    winner: str  # "A" | "B" | "tie"
    reason: str


class EvalCompareResponse(BaseModel):
    query: str
    results: list[EvalStrategyResultOut]
    judges: list[EvalJudgeOut] = Field(default_factory=list)
    judge_enabled: bool
    judge_reason: str | None = None  # 例:LLM 未配置 / 后台 flag 关闭
