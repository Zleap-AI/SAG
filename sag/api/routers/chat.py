"""
对话 API - Researcher Agent 接口

提供智能对话问答功能
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sag.api.deps import get_db
from sag.api.schemas.chat import ChatFeedback, ChatRequest
from sag.api.schemas.common import SuccessResponse
from sag.utils import get_logger

router = APIRouter()
logger = get_logger("api.chat")


@router.post("/chat/message")
async def chat_message(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    发送对话消息（流式响应）

    **功能**：
    - 智能理解用户问题
    - 主动搜索相关信息（SAG引擎）
    - 生成高质量回答
    - 实时展示思考过程

    **参数**：
    - query: 用户问题
    - source_config_ids: 信息源ID列表（支持多源）
    - mode: 模式（quick快速 / deep深度）
    - context: 对话历史（最近20条）
    - params: AI参数（top_k, result_style等）

    **流式输出格式（SSE）**：
    ```
    data: {"type": "stage", "stage": "understanding"}
    data: {"type": "thinking", "content": "正在分析问题...", "stage": "understanding"}
    data: {"type": "content", "content": "根据搜索..."}
    data: {"type": "done", "stats": {"events_found": 8, "confidence": 0.85}}
    ```

    **type 类型**：
    - stage: 阶段切换（understanding/planning/researching/evaluating/synthesizing）
    - thinking: 思考过程（展示Agent的推理）
    - content: 回答内容（逐字输出）
    - done: 完成（包含统计信息）
    - error: 错误信息
    """
    from sag.core.agent.researcher import ResearcherAgent

    # 验证信息源
    if not request.source_config_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须选择至少一个信息源"
        )

    # 验证模式
    if request.mode not in ["quick", "deep"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode 必须是 'quick' 或 'deep'"
        )

    # 准备对话上下文（转换为字典格式）
    context_list = None
    if request.context:
        context_list = [msg.model_dump() for msg in request.context]

    # 创建 Agent
    try:
        agent = ResearcherAgent(
            source_config_ids=request.source_config_ids,
            mode=request.mode,
            conversation_history=context_list,
        )
    except Exception as e:
        logger.error(f"创建 ResearcherAgent 失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent 初始化失败：{str(e)}"
        )

    # 流式响应生成器
    async def generate():
        """SSE 流式生成器"""
        try:
            async for chunk in agent.chat(
                query=request.query,
                **(request.params or {})
            ):
                # SSE 格式：data: {json}\n\n
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.error(f"对话执行失败: {e}", exc_info=True)
            # 发送错误信息
            error_chunk = {
                "type": "error",
                "content": f"执行失败：{str(e)}"
            }
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"

    # 返回 SSE 流
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        }
    )


@router.post("/chat/feedback", response_model=SuccessResponse[dict])
async def submit_feedback(
    feedback: ChatFeedback,
    db: AsyncSession = Depends(get_db),
):
    """
    提交对话反馈

    **功能**：
    - 收集用户对AI回答的反馈
    - 用于改进 Agent 质量
    - 优化搜索策略

    **参数**：
    - message_id: 消息ID
    - rating: 评分（1-5星）
    - feedback_type: 反馈类型（helpful/not_helpful/inaccurate/incomplete）
    - comment: 用户评论（可选）

    **返回**：
    - 成功消息
    """
    # TODO: 未来可以保存反馈到数据库，用于质量分析
    logger.info(
        f"收到反馈: message_id={feedback.message_id}, "
        f"rating={feedback.rating}, type={feedback.feedback_type}"
    )

    return SuccessResponse(
        data={"message_id": feedback.message_id},
        message="反馈已提交，感谢您的反馈！"
    )


@router.get("/chat/sessions", response_model=SuccessResponse[list])
async def list_chat_sessions(
    db: AsyncSession = Depends(get_db),
):
    """
    获取对话会话列表

    **功能**：
    - 查询用户的历史对话会话
    - 支持恢复历史对话

    **返回**：
    - 会话列表
    """
    # TODO: 未来实现会话持久化
    return SuccessResponse(
        data=[],
        message="对话会话列表（开发中）"
    )
