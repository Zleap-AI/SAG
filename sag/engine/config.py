"""
引擎配置类

定义所有阶段的配置选项
"""

from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel, Field

from sag.engine.enums import OutputMode
from sag.modules.extract.config import ExtractBaseConfig
from sag.modules.load.config import ConversationLoadConfig, DocumentLoadConfig
from sag.modules.search.config import SearchBaseConfig


class ModelConfig(BaseModel):
    """LLM配置"""

    api_key: Optional[str] = Field(default=None, description="API密钥")
    model: str = Field(default="sophnet/Qwen3-30B-A3B-Thinking-2507", description="模型名称")
    base_url: Optional[str] = Field(default=None, description="API基础URL")
    timeout: int = Field(default=300, description="超时时间（秒）")
    max_retries: int = Field(default=3, description="最大重试次数")
    temperature: float = Field(default=0.3, description="生成温度")
    with_retry: bool = Field(default=True, description="是否启用重试")


class OutputConfig(BaseModel):
    """输出配置"""

    mode: OutputMode = Field(default=OutputMode.FULL, description="输出模式（ID或完整内容）")
    format: str = Field(default="json", description="输出格式（json/markdown）")
    include_logs: bool = Field(default=True, description="是否在输出中包含日志")
    print_logs: bool = Field(default=True, description="是否打印日志到控制台")
    export_path: Optional[Path] = Field(default=None, description="导出文件路径")
    pretty: bool = Field(default=True, description="是否美化输出")


class TaskConfig(BaseModel):
    """任务配置（统一配置）"""

    # 任务基本信息
    task_name: str = Field(default="SAG任务", description="任务名称")
    task_description: Optional[str] = Field(default=None, description="任务描述")

    # 运行时上下文 (全局配置)
    source_config_id: Optional[str] = Field(default=None, description="信息源ID")
    source_name: Optional[str] = Field(default=None, description="信息源名称")
    background: Optional[str] = Field(default=None, description="全局背景信息（可被阶段覆盖）")

    # 各阶段配置 (基础配置)
    load: Optional[Union[ConversationLoadConfig, DocumentLoadConfig]] = Field(
        default=None, description="加载阶段配置（文档或会话加载）"
    )
    extract: Optional[ExtractBaseConfig] = Field(default=None, description="提取阶段配置")
    search: Optional[SearchBaseConfig] = Field(default=None, description="搜索阶段配置")

    # 输出配置
    output: OutputConfig = Field(default_factory=OutputConfig, description="输出配置")

    # 通用配置
    fail_fast: bool = Field(default=False, description="是否快速失败")
