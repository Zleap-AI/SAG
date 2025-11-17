"""统一流程 API

提供 Load + Extract + Search 的统一调用接口
"""

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sag import SAGEngine, ExtractBaseConfig, DocumentLoadConfig, SearchBaseConfig
from sag.api.deps import get_db
from sag.api.schemas.common import SuccessResponse, TaskStatusResponse
from sag.api.schemas.pipeline import PipelineRequest, PipelineResponse
from sag.api.services.pipeline_service import PipelineService

router = APIRouter()


@router.post(
    "/pipeline/run",
    response_model=SuccessResponse[TaskStatusResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_pipeline(
    request: PipelineRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    运行完整流程（异步）

    **功能**：
    - 统一执行 Load → Extract → Search 流程
    - 异步执行，立即返回 task_id，不阻塞请求
    - 支持灵活配置各阶段参数，可选择性执行：
      - 只执行 Load：获取文档段落
      - Load + Extract：生成事项和实体
      - 完整流程：包含搜索和重排

    **请求参数**：
    ```json
    {
      "source_config_id": "source-001",
      "task_name": "处理AI文档",
      "task_description": "对AI技术文档进行分析和提取",
      "background": "这是AI技术文档集合，包含深度学习相关内容",

      "load": {
        "path": "./docs/ai_article.md",
        "max_tokens": 8000,
        "auto_vector": true,
        "min_content_length": 100,
        "merge_short_sections": true
      },

      "extract": {
        "max_concurrency": 10,
        "auto_vector": true,
        "custom_entity_types": [
          {
            "name": "算法",
            "description": "机器学习算法名称"
          }
        ]
      },

      "search": {
        "query": "深度学习的最新进展",
        "enable_query_rewrite": true,
        "return_type": "event",
        "recall": {
          "use_fast_mode": true,
          "vector_top_k": 15,
          "max_entities": 25,
          "max_events": 60
        },
        "expand": {
          "enabled": true,
          "max_hops": 3,
          "entities_per_hop": 10
        },
        "rerank": {
          "strategy": "rrf",
          "max_results": 10,
          "rrf_k": 60
        }
      }
    }
    ```

    **返回值**：
    ```json
    {
      "success": true,
      "data": {
        "task_id": "task-uuid-xxx",
        "status": "pending",
        "message": "任务已创建，正在执行中..."
      },
      "message": "流程已启动"
    }
    ```

    **后续操作**：
    1. 查询任务状态：`GET /tasks/{task_id}`
       - 返回状态：pending, running, completed, failed
       - 包含进度信息和执行结果

    2. 列出所有任务：`GET /tasks`
       - 支持按 source_config_id、status 筛选
       - 支持分页查询

    3. 获取任务统计：`GET /tasks/stats`
       - 按状态和类型的统计信息

    **使用场景**：
    - 大规模文档处理（推荐使用异步模式）
    - 需要追踪执行进度的长时间任务
    - 批量文档的自动化处理流程

    **注意事项**：
    - 各阶段配置均为可选，未配置则跳过该阶段
    - load.path 和 load.article_id 二选一
    - background 可在全局设置，也可在各阶段单独设置
    - 建议大规模数据处理使用此接口，小规模可使用 /pipeline/run-sync
    """
    service = PipelineService(db)

    # 创建任务并在后台执行
    task_id = await service.create_task(request)
    background_tasks.add_task(service.execute_pipeline, task_id, request)

    return SuccessResponse(
        data=TaskStatusResponse(
            task_id=task_id,
            status="pending",
            message="任务已创建，正在执行中...",
        ),
        message="流程已启动",
    )


@router.post(
    "/pipeline/run-sync",
    response_model=SuccessResponse[PipelineResponse],
)
async def run_pipeline_sync(
    request: PipelineRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    同步运行完整流程

    **功能**：
    - 同步执行，等待所有阶段完成后返回结果
    - 适合小规模数据处理
    - 不建议用于大规模文档处理

    **注意**：
    - 可能需要较长时间
    - 建议使用异步接口 /pipeline/run
    """
    service = PipelineService(db)
    result = await service.execute_pipeline_sync(request)
    return SuccessResponse(data=result, message="流程执行完成")


@router.post(
    "/pipeline/load",
    response_model=SuccessResponse[dict],
)
async def run_load_only(
    source_config_id: str,
    path: str,
    background: Optional[str] = None,
    recursive: bool = True,
    pattern: str = "*.md",
    db: AsyncSession = Depends(get_db),
):
    """
    只执行 Load 阶段

    **功能**：
    - 加载文档并生成摘要、切块
    - 返回 article_id 供后续使用
    """
    # 创建引擎并执行 Load
    engine = SAGEngine(source_config_id=source_config_id)
    await engine.load_async(
        DocumentLoadConfig(
            source_config_id=source_config_id,
            path=path,
            recursive=recursive,
            pattern=pattern,
        )
    )

    result = engine.get_result()

    if result.is_success() and result.load_result:
        return SuccessResponse(
            data={
                "article_id": result.article_id,
                "sections": result.load_result.data_ids,
                "stats": result.load_result.stats,
            },
            message="Load 阶段完成",
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.error or "Load 失败",
        )


@router.post(
    "/pipeline/extract",
    response_model=SuccessResponse[dict],
)
async def run_extract_only(
    source_config_id: str,
    article_id: str,
    parallel: bool = True,
    max_concurrency: int = 10,
    db: AsyncSession = Depends(get_db),
):
    """
    只执行 Extract 阶段

    **前提**：
    - 文档已通过 Load 阶段处理

    **参数**：
    - source_config_id: 信息源ID
    - article_id: 文章ID
    """
    engine = SAGEngine(source_config_id=source_config_id)
    engine._article_id = article_id

    await engine.extract_async(
        ExtractBaseConfig(
            parallel=parallel,
            max_concurrency=max_concurrency,
        )
    )

    result = engine.get_result()

    if result.is_success() and result.extract_result:
        return SuccessResponse(
            data={
                "events": result.extract_result.data_ids,
                "stats": result.extract_result.stats,
            },
            message="Extract 阶段完成",
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.error or "Extract 失败",
        )


class SearchRequest(BaseModel):
    """搜索请求"""
    source_config_id: Optional[str] = None  # 单个信息源ID（向后兼容）
    source_config_ids: Optional[List[str]] = None  # 多个信息源ID列表
    query: str

    # === 功能开关 ===
    enable_query_rewrite: Optional[bool] = None
    use_fast_mode: Optional[bool] = None

    # === Recall参数 ===
    vector_top_k: Optional[int] = None
    vector_candidates: Optional[int] = None
    entity_similarity_threshold: Optional[float] = None
    event_similarity_threshold: Optional[float] = None
    max_entities: Optional[int] = None
    max_events: Optional[int] = None
    entity_weight_threshold: Optional[float] = None
    final_entity_count: Optional[int] = None

    # === Expand参数 ===
    expand_enabled: Optional[bool] = None
    max_hops: Optional[int] = None
    entities_per_hop: Optional[int] = None
    weight_change_threshold: Optional[float] = None
    expand_event_similarity_threshold: Optional[float] = None
    min_events_per_hop: Optional[int] = None
    max_events_per_hop: Optional[int] = None

    # === Rerank参数 ===
    use_pagerank: Optional[bool] = None  # 是否使用PageRank（否则使用RRF）
    strategy: Optional[str] = None  # "pagerank" or "rrf"（优先级高于use_pagerank）
    score_threshold: Optional[float] = None
    max_results: Optional[int] = None
    max_key_recall_results: Optional[int] = None  # Step1 Key召回的最大事项/段落数
    max_query_recall_results: Optional[int] = None  # Step2 Query召回的最大事项/段落数
    pagerank_section_top_k: Optional[int] = None
    pagerank_damping_factor: Optional[float] = None
    pagerank_max_iterations: Optional[int] = None
    rrf_k: Optional[int] = None


@router.post(
    "/pipeline/search",
    response_model=SuccessResponse[dict],
)
async def run_search_only(
    request: SearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    只执行 Search 阶段

    **前提**：
    - 已有事项数据（通过 Load + Extract 生成）

    **参数**：
    - source_config_id / source_config_ids: 单个或多个信息源ID
    - query: 查询文本
    - 以及 Recall/Expand/Rerank 的配置参数
    """
    # 验证：至少提供一个 source_config_id 或 source_config_ids
    if not request.source_config_id and not request.source_config_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须提供 source_config_id 或 source_config_ids 参数"
        )

    # 兼容处理：统一转为 source_config_ids
    source_config_ids = request.source_config_ids if request.source_config_ids else [
        request.source_config_id]
    primary_source_config_id = source_config_ids[0]  # 用于创建引擎

    engine = SAGEngine(source_config_id=primary_source_config_id)

    # 构建新的配置结构
    from sag.modules.search.config import RecallConfig, ExpandConfig, RerankConfig, RerankStrategy, SearchConfig

    # Recall配置
    recall_dict = {}
    recall_mapping = {
        "use_fast_mode": "use_fast_mode",
        "vector_top_k": "vector_top_k",
        "vector_candidates": "vector_candidates",
        "entity_similarity_threshold": "entity_similarity_threshold",
        "event_similarity_threshold": "event_similarity_threshold",
        "max_entities": "max_entities",
        "max_events": "max_events",
        "entity_weight_threshold": "entity_weight_threshold",
        "final_entity_count": "final_entity_count",
    }
    for req_param, config_param in recall_mapping.items():
        value = getattr(request, req_param, None)
        if value is not None:
            recall_dict[config_param] = value

    # Expand配置
    expand_dict = {}
    expand_mapping = {
        "expand_enabled": "enabled",
        "max_hops": "max_hops",
        "entities_per_hop": "entities_per_hop",
        "weight_change_threshold": "weight_change_threshold",
        "expand_event_similarity_threshold": "event_similarity_threshold",
        "min_events_per_hop": "min_events_per_hop",
        "max_events_per_hop": "max_events_per_hop",
    }
    for req_param, config_param in expand_mapping.items():
        value = getattr(request, req_param, None)
        if value is not None:
            expand_dict[config_param] = value

    # Rerank配置
    rerank_dict = {}

    # 处理 use_pagerank 到 strategy 的转换
    # 如果没有明确指定 strategy，则根据 use_pagerank 来决定
    if request.strategy is None and request.use_pagerank is not None:
        request.strategy = "pagerank" if request.use_pagerank else "rrf"

    rerank_mapping = {
        "strategy": "strategy",
        "score_threshold": "score_threshold",
        "max_results": "max_results",
        "max_key_recall_results": "max_key_recall_results",
        "max_query_recall_results": "max_query_recall_results",
        "pagerank_section_top_k": "pagerank_section_top_k",
        "pagerank_damping_factor": "pagerank_damping_factor",
        "pagerank_max_iterations": "pagerank_max_iterations",
        "rrf_k": "rrf_k",
    }
    for req_param, config_param in rerank_mapping.items():
        value = getattr(request, req_param, None)
        if value is not None:
            # strategy需要转换为枚举
            if config_param == "strategy" and value:
                rerank_dict[config_param] = RerankStrategy(value)
            else:
                rerank_dict[config_param] = value

    # 直接构建完整的 SearchConfig（包含 source_config_ids）
    search_config = SearchConfig(
        query=request.query,
        source_config_ids=source_config_ids,  # 传递多源支持
        enable_query_rewrite=request.enable_query_rewrite if request.enable_query_rewrite is not None else True,
        recall=RecallConfig(**recall_dict) if recall_dict else RecallConfig(),
        expand=ExpandConfig(**expand_dict) if expand_dict else ExpandConfig(),
        rerank=RerankConfig(**rerank_dict) if rerank_dict else RerankConfig(),
    )

    # 使用 SearchBaseConfig，engine.search_async() 会自动添加 source_config_id 转换为 SearchConfig
    await engine.search_async(search_config)

    result = engine.get_result()

    # 打印调试信息
    print(f"🔍 搜索结果调试信息:")
    print(f"  - 任务整体状态: {result.status}")
    print(
        f"  - 搜索阶段状态: {result.search_result.status if result.search_result else None}")

    # 只要搜索阶段成功就返回结果，不管整体任务状态
    if result.search_result and result.search_result.status == "success":
        try:
            print(f"  - data_full 长度: {len(result.search_result.data_full)}")

            # 扩展事项内容（补充实体和原文引用）
            from sag.modules.search.enricher import EventEnricher
            enricher = EventEnricher(db)
            events = await enricher.enrich_events(result.search_result.data_full)

            # 从stats中提取clues（now from-to格式的详细线索列表）
            clues = result.search_result.stats.get(
                "clues", []) if result.search_result.stats else []

            print(f"✅ 成功处理 {len(events)} 个事项（已扩展实体和引用）")
            print(f"📋 Clues信息: 总共 {len(clues)} 条线索"
                  f" (recall={len([c for c in clues if c.get('stage') == 'recall'])}, "
                  f"expand={len([c for c in clues if c.get('stage') == 'expand'])}, "
                  f"rerank={len([c for c in clues if c.get('stage') == 'rerank'])})")

            return SuccessResponse(
                data={
                    "events": events,
                    "clues": clues,  # 返回 from-to 格式的详细线索
                    "stats": {
                        "matched_count": result.search_result.stats.get("matched_count", len(events))
                    },
                },
                message="Search 完成",
            )
        except Exception as e:
            print(f"❌ 数据处理错误: {e}")
            import traceback
            traceback.print_exc()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"数据处理失败: {str(e)}",
            )
    else:
        error_msg = result.search_result.error if result.search_result else "Search 失败"
        print(f"❌ 搜索失败: {error_msg}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg,
        )


class SummarizeRequest(BaseModel):
    """AI总结请求"""
    source_config_id: str
    query: str
    event_ids: List[str]


@router.post("/pipeline/summarize")
async def summarize_search_results(
    request: SummarizeRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    AI智能总结搜索结果（流式输出）

    根据搜索结果的事项ID，查询完整数据并使用AI进行总结

    Args:
        source_config_id: 信息源ID
        query: 用户查询
        event_ids: 事项ID列表

    Returns:
        Server-Sent Events 流式响应
    """
    from sag.core.agent import SummarizerAgent
    from sag.db.models import SourceEvent
    from sqlalchemy import select

    try:
        # 1. 从数据库查询事项完整数据
        events_data = []

        for event_id in request.event_ids:
            try:
                # 查询事项
                result = await db.execute(
                    select(SourceEvent).where(SourceEvent.id == event_id)
                )
                event = result.scalar_one_or_none()

                if not event:
                    print(f"⚠️  事项不存在: {event_id}")
                    continue

                # 构建事项数据（references 已经在 event.references JSON字段中）
                event_data = {
                    "id": event.id,
                    "title": event.title,
                    "summary": event.summary,
                    "content": event.content,
                    "references": event.references or [],  # references 是 JSON 字段
                }
                events_data.append(event_data)

            except Exception as e:
                print(f"⚠️  查询事项失败 {event_id}: {e}")
                import traceback
                traceback.print_exc()
                continue

        if not events_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="未找到有效的事项数据"
            )

        print(f"✅ 成功加载 {len(events_data)} 条事项用于总结")

        # 2. 创建 SummarizerAgent 并执行
        agent = SummarizerAgent(events=events_data)

        # 3. 流式生成响应
        async def generate():
            try:
                async for chunk in agent.run(request.query):
                    # SSE 格式
                    if chunk.get("reasoning"):
                        yield f"event: thinking\ndata: {chunk['reasoning']}\n\n"
                    if chunk.get("content"):
                        yield f"event: content\ndata: {chunk['content']}\n\n"

            except Exception as e:
                print(f"❌ 总结失败: {e}")
                import traceback
                traceback.print_exc()
                yield f"event: error\ndata: {str(e)}\n\n"
            finally:
                yield "event: done\ndata: \n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 禁用nginx缓冲
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ 总结请求处理失败: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"总结请求失败: {str(e)}"
        )
