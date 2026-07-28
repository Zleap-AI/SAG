"""Dify 外部知识库检索协议。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DifyRetrievalSetting(BaseModel):
    top_k: int = Field(default=4, ge=1, le=50)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)


class DifyRetrievalRequest(BaseModel):
    knowledge_id: str = Field(default="", max_length=64)
    query: str = Field(default="", max_length=4000)
    retrieval_setting: DifyRetrievalSetting = Field(
        default_factory=DifyRetrievalSetting
    )
    metadata_condition: dict[str, Any] | None = None


class DifyRetrievalRecord(BaseModel):
    content: str
    title: str
    score: float
    metadata: dict[str, Any]


class DifyRetrievalResponse(BaseModel):
    records: list[DifyRetrievalRecord] = Field(default_factory=list)
