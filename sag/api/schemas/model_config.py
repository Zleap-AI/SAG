"""模型配置相关的 Pydantic Schema（LLM、Embedding等）"""

from decimal import Decimal
from typing import Any, Dict, Optional
from datetime import datetime

from pydantic import BaseModel, Field


class ModelConfigBase(BaseModel):
    """模型配置基础Schema"""
    
    name: str = Field(..., description="配置名称")
    description: Optional[str] = Field(None, description="配置说明")
    
    # 双维度分类
    type: str = Field(default='llm', description="模型类型：llm/embedding/rerank等")
    scenario: str = Field(default='general', description="使用场景：general/extract/search/chat/summary等")
    
    # API配置
    provider: Optional[str] = Field(None, description="供应商标识（如：openai, 302ai）")
    api_key: str = Field(..., description="API密钥")
    base_url: str = Field(..., description="API基础URL")
    model: str = Field(..., description="模型名称")
    
    # 行为参数
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="温度参数")
    max_tokens: int = Field(default=8000, ge=1, description="最大输出token数")
    top_p: float = Field(default=1.0, ge=0.0, le=1.0, description="top_p参数")
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0, description="频率惩罚")
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0, description="存在惩罚")
    
    # 可靠性参数
    timeout: int = Field(default=600, ge=1, description="超时时间(秒)")
    max_retries: int = Field(default=3, ge=0, description="最大重试次数")
    
    # 扩展数据（模型特定参数，如 embedding 的 dimensions）
    extra_data: Optional[Dict[str, Any]] = Field(None, description="扩展数据")
    
    # 状态和优先级
    is_active: bool = Field(default=True, description="是否启用")
    priority: int = Field(default=0, description="优先级（数字越大越优先）")


class ModelConfigCreate(ModelConfigBase):
    """创建模型配置"""
    pass


class ModelConfigUpdate(BaseModel):
    """更新模型配置（所有字段可选）"""
    
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    scenario: Optional[str] = None
    
    provider: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(None, ge=1)
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0)
    frequency_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    presence_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    
    timeout: Optional[int] = Field(None, ge=1)
    max_retries: Optional[int] = Field(None, ge=0)
    extra_data: Optional[Dict[str, Any]] = None
    
    is_active: Optional[bool] = None
    priority: Optional[int] = None


class ModelConfigResponse(ModelConfigBase):
    """模型配置响应"""
    
    id: str
    created_time: datetime
    updated_time: datetime
    created_by: Optional[str] = None
    
    class Config:
        from_attributes = True

