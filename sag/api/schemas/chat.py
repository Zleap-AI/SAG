"""对话相关 Schema"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """对话消息"""

    role: str = Field(..., description="角色：user/assistant")
    content: str = Field(..., description="消息内容")
    thinking: Optional[List[str]] = Field(default=None, description="思考过程")
    sources: Optional[List[str]] = Field(default=None, description="引用的信息源ID")
    stats: Optional[Dict[str, Any]] = Field(default=None, description="统计信息")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="时间戳")


class ChatRequest(BaseModel):
    """对话请求"""

    query: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="用户问题"
    )
    source_config_ids: List[str] = Field(
        ...,
        min_items=1,
        max_items=10,
        description="信息源ID列表（至少1个，最多10个）"
    )
    mode: str = Field(
        default="quick",
        pattern="^(quick|deep)$",
        description="模式：quick(快速) 或 deep(深度)"
    )
    context: Optional[List[ChatMessage]] = Field(
        default=None,
        max_items=20,
        description="对话历史（最近20条，用于上下文理解）"
    )
    params: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="AI参数：top_k(检索条数), result_style(结果风格)等"
    )


class ChatResponse(BaseModel):
    """对话响应（非流式）"""

    message: ChatMessage = Field(..., description="AI回复消息")
    search_stats: Optional[Dict] = Field(default=None, description="搜索统计")
    confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="回答置信度")
    thinking_process: Optional[List[str]] = Field(
        default=None, description="完整思考过程")


class ChatFeedback(BaseModel):
    """对话反馈"""

    message_id: str = Field(..., description="消息ID")
    rating: int = Field(..., ge=1, le=5, description="评分 1-5星")
    feedback_type: str = Field(
        ...,
        description="反馈类型：helpful(有帮助)/not_helpful(无帮助)/inaccurate(不准确)/incomplete(不完整)"
    )
    comment: Optional[str] = Field(
        default=None,
        max_length=500,
        description="用户评论（可选）"
    )


class ChatSession(BaseModel):
    """对话会话"""

    session_id: str = Field(..., description="会话ID")
    source_config_ids: List[str] = Field(..., description="当前使用的信息源")
    mode: str = Field(..., description="对话模式")
    message_count: int = Field(default=0, description="消息数量")
    created_time: datetime = Field(default_factory=datetime.utcnow)
    updated_time: datetime = Field(default_factory=datetime.utcnow)
