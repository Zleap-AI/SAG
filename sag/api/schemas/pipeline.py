"""流程相关 Schema"""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from sag.engine.config import ModelConfig, OutputConfig
from sag.modules.extract.config import ExtractBaseConfig
from sag.modules.load.config import ConversationLoadConfig, DocumentLoadConfig
from sag.modules.search.config import SearchBaseConfig


class PipelineRequest(BaseModel):
    """流程请求"""

    # 任务基本信息
    task_name: str = Field(default="SAG任务", description="任务名称")
    task_description: Optional[str] = Field(default=None, description="任务描述")

    # 运行时上下文
    source_config_id: str = Field(..., description="信息源ID")
    source_name: Optional[str] = Field(default=None, description="信息源名称")
    background: Optional[str] = Field(
        default=None, description="全局背景信息（可被阶段覆盖）"
    )

    # 各阶段配置
    # 注意：Union 顺序很重要，ConversationLoadConfig 在前，让它先尝试匹配
    load: Optional[Union[ConversationLoadConfig, DocumentLoadConfig]] = Field(
        default=None, description="加载阶段配置（文档或会话加载）"
    )
    extract: Optional[ExtractBaseConfig] = Field(
        default=None, description="提取阶段配置")
    search: Optional[SearchBaseConfig] = Field(
        default=None, description="搜索阶段配置")

    # LLM和输出配置
    llm: Optional[ModelConfig] = Field(default=None, description="LLM配置")
    output: Optional[OutputConfig] = Field(
        default_factory=OutputConfig, description="输出配置"
    )

    # 通用配置
    fail_fast: bool = Field(default=False, description="是否快速失败")


class PipelineResponse(BaseModel):
    """流程响应"""

    task_id: str
    task_name: str
    status: str
    source_config_id: str
    article_id: Optional[str] = None

    # 各阶段结果
    load_result: Optional[Dict[str, Any]] = None
    extract_result: Optional[Dict[str, Any]] = None
    search_result: Optional[Dict[str, Any]] = None

    # 统计信息
    stats: Dict[str, Any] = Field(default_factory=dict)
    logs: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None

    # 时间信息
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration: Optional[float] = None
