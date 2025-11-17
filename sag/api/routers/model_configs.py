"""模型配置管理 API（LLM、Embedding等）"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag.api.deps import get_db
from sag.api.schemas.common import SuccessResponse
from sag.api.schemas.model_config import (
    ModelConfigCreate,
    ModelConfigResponse,
    ModelConfigUpdate,
)
from sag.db.models import ModelConfig

router = APIRouter()


@router.get("", response_model=SuccessResponse[List[ModelConfigResponse]])
async def list_model_configs(
    type: Optional[str] = Query(None, description="模型类型筛选：llm/embedding等"),
    scenario: Optional[str] = Query(None, description="场景筛选：general/extract/search/chat/summary等"),
    is_active: Optional[bool] = Query(None, description="状态筛选"),
    db: AsyncSession = Depends(get_db),
):
    """
    列出模型配置
    
    Args:
        type: 模型类型筛选（可选）
        scenario: 场景筛选（可选）
        is_active: 状态筛选（可选）
    
    Returns:
        配置列表（按 type, scenario, priority, created_time 排序）
    """
    query = select(ModelConfig)
    
    if type:
        query = query.where(ModelConfig.type == type)
    if scenario:
        query = query.where(ModelConfig.scenario == scenario)
    if is_active is not None:
        query = query.where(ModelConfig.is_active == is_active)
    
    query = query.order_by(
        ModelConfig.type,
        ModelConfig.scenario,
        ModelConfig.priority.desc(),
        ModelConfig.created_time.desc()
    )
    
    result = await db.execute(query)
    configs = result.scalars().all()
    
    return SuccessResponse(
        data=[ModelConfigResponse.model_validate(c) for c in configs],
        message=f"查询成功，共 {len(configs)} 条配置"
    )


@router.get("/{config_id}", response_model=SuccessResponse[ModelConfigResponse])
async def get_model_config(
    config_id: str,
    db: AsyncSession = Depends(get_db),
):
    """获取单个模型配置"""
    config = await db.get(ModelConfig, config_id)
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"配置不存在: {config_id}"
        )
    
    return SuccessResponse(
        data=ModelConfigResponse.model_validate(config),
        message="查询成功"
    )


@router.post("", response_model=SuccessResponse[ModelConfigResponse], status_code=status.HTTP_201_CREATED)
async def create_model_config(
    config_data: ModelConfigCreate,
    db: AsyncSession = Depends(get_db),
):
    """创建模型配置"""
    # 创建配置
    config = ModelConfig(
        id=str(uuid.uuid4()),
        **config_data.model_dump()
    )
    
    db.add(config)
    await db.commit()
    await db.refresh(config)
    
    return SuccessResponse(
        data=ModelConfigResponse.model_validate(config),
        message="创建成功"
    )


@router.put("/{config_id}", response_model=SuccessResponse[ModelConfigResponse])
async def update_model_config(
    config_id: str,
    config_data: ModelConfigUpdate,
    db: AsyncSession = Depends(get_db),
):
    """更新模型配置"""
    config = await db.get(ModelConfig, config_id)
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"配置不存在: {config_id}"
        )
    
    # 更新字段（只更新非None的字段）
    for field, value in config_data.model_dump(exclude_unset=True).items():
        setattr(config, field, value)
    
    await db.commit()
    await db.refresh(config)
    
    return SuccessResponse(
        data=ModelConfigResponse.model_validate(config),
        message="更新成功"
    )


@router.delete("/{config_id}")
async def delete_model_config(
    config_id: str,
    db: AsyncSession = Depends(get_db),
):
    """删除模型配置"""
    config = await db.get(ModelConfig, config_id)
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"配置不存在: {config_id}"
        )
    
    await db.delete(config)
    await db.commit()
    
    return SuccessResponse(
        data={"id": config_id},
        message="删除成功"
    )


@router.post("/{config_id}/test")
async def test_model_config(
    config_id: str,
    test_message: str = "Hello, this is a test message.",
    db: AsyncSession = Depends(get_db),
):
    """
    测试模型配置
    
    发送一条测试消息，验证配置是否有效（仅支持 LLM 类型）
    """
    config = await db.get(ModelConfig, config_id)
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"配置不存在: {config_id}"
        )
    
    # 验证模型类型（仅支持 LLM）
    if config.type != 'llm':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"测试功能仅支持 LLM 类型，当前类型: {config.type}"
        )
    
    # 使用该配置创建临时客户端
    from sag.core.ai.factory import create_llm_client
    from sag.core.ai.models import LLMMessage, LLMRole
    
    try:
        # 创建客户端
        client = await create_llm_client(
            scenario=config.scenario,
            model_config={
                'model': config.model,
                'api_key': config.api_key,
                'base_url': config.base_url,
                'temperature': float(config.temperature),
                'max_tokens': config.max_tokens,
                'timeout': config.timeout,
                'max_retries': config.max_retries,
            }
        )
        
        # 发送测试消息
        response = await client.chat([
            LLMMessage(role=LLMRole.USER, content=test_message)
        ])
        
        return SuccessResponse(
            data={
                "success": True,
                "model": response.model,
                "response_preview": response.content[:200],
                "tokens_used": response.total_tokens,
            },
            message="配置测试成功"
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"配置测试失败: {str(e)}"
        )

