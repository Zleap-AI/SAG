# 搜索模块（Search Module）

简洁、清晰、易于调适的SAG搜索引擎。

## 🎯 核心特性

- **只保留SAG引擎** - 移除了LLM和RAG处理器
- **三阶段流程** - Recall → Expand → Rerank
- **完整线索追踪** - 支持前端关系图谱展示
- **白盒化检索** - 全过程可追溯
- **具象化命名** - 代码可读性强

## 📁 目录结构

```
search/
├── __init__.py          # 导出接口
├── config.py            # 配置文件
├── searcher.py          # 搜索器入口
├── tracker.py      # 线索追踪器
├── recall.py            # 实体召回
├── expand.py            # 实体扩展
├── ranking/             # 事项排序策略
│   ├── pagerank.py      # PageRank排序
│   └── rrf.py           # RRF融合排序
└── utils/               # 工具函数
```

## 🔄 三阶段流程

### 1. Recall（实体召回）

从query召回相关实体。

**核心算法**：8步骤复合搜索
1. query找key - 向量相似度
2. key找event - SQL关联
3. query再找event - 向量相似度
4. 过滤Event - 取交集
5-8. 权重计算和反向传播

**输入**：
- query: 查询文本
- source_config_id: 数据源ID

**输出**：
- 相关实体列表（带权重）
- 召回线索（query → entity）

### 2. Expand（实体扩展）

通过多跳关系发现更多相关实体。

**核心算法**：多跳循环搜索
- 基于召回的实体执行多跳扩展
- 每跳发现新的相关实体
- 支持收敛检测

**输入**：
- 召回的实体列表

**输出**：
- 扩展后的实体列表（带权重和跳数）
- 扩展线索（entity → entity）

### 3. Rerank（事项重排）

基于实体列表检索和排序最终事项。

**两种策略**：
- **PageRank**：段落搜索 + PageRank算法（精准）
- **RRF**：Embedding + BM25 融合（快速）

**输入**：
- 扩展后的实体列表

**输出**：
- 排序后的事项列表
- 重排线索（entity → event）

## 💻 使用示例

### 基础用法

```python
from sag.modules.search import SAGSearcher, SearchConfig

# 初始化搜索器
searcher = SAGSearcher(llm_client, prompt_manager)

# 配置搜索参数
config = SearchConfig(
    query="人工智能的最新进展",
    source_config_id="source_123",
)

# 执行搜索
result = await searcher.search(config)

# 使用结果
print(f"找到 {len(result['events'])} 个事项")
print(f"生成 {len(result['clues'])} 条线索")
```

### 高级配置

```python
from sag.modules.search import (
    SearchConfig,
    RecallConfig,
    ExpandConfig,
    RerankConfig,
    RerankStrategy,
)

config = SearchConfig(
    query="人工智能",
    source_config_id="source_123",
    
    # 召回配置
    recall=RecallConfig(
        vector_top_k=20,
        max_entities=30,
        entity_similarity_threshold=0.4,
    ),
    
    # 扩展配置
    expand=ExpandConfig(
        enabled=True,
        max_hops=3,
        entities_per_hop=10,
    ),
    
    # 重排配置
    rerank=RerankConfig(
        strategy=RerankStrategy.PAGERANK,
        max_results=10,
    )
)

result = await searcher.search(config)
```

## 📊 返回结果

```python
{
    "events": [SourceEvent, ...],  # 事项列表
    "clues": [                      # 线索列表
        {
            "id": "clue_uuid",
            "stage": "recall",      # recall/expand/rerank
            "from": {...},          # 起点节点
            "to": {...},            # 终点节点
            "confidence": 0.92,     # 置信度
            "relation": "语义相似",  # 关系类型
            "metadata": {...}       # 元数据
        },
        ...
    ],
    "stats": {                      # 统计信息
        "recall": {...},
        "expand": {...},
        "rerank": {...}
    },
    "query": {                      # 查询信息
        "original": "...",
        "current": "...",
        "rewritten": false
    }
}
```

## 🎨 前端集成

线索数据支持直接用于 [relation-graph](https://www.relation-graph.com/#/docs/start) 展示：

```typescript
import RelationGraph from 'relation-graph';

function renderSearchGraph(searchResult) {
  const { clues } = searchResult;
  
  const nodes = [];
  const links = [];
  
  clues.forEach(clue => {
    // 添加节点
    if (!nodes.find(n => n.id === clue.from.id)) {
      nodes.push({
        id: clue.from.id,
        text: clue.from.content,
        nodeShape: getShapeByType(clue.from.type),
      });
    }
    
    // 添加边
    links.push({
      from: clue.from.id,
      to: clue.to.id,
      text: clue.relation,
      lineWidth: clue.confidence * 3,
    });
  });
  
  graphInstance.setJsonData({ nodes, links });
}
```

## ⚙️ 配置说明

### RecallConfig（召回配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| vector_top_k | 15 | 向量检索返回数量 |
| vector_candidates | 100 | 向量检索候选池大小 |
| entity_similarity_threshold | 0.4 | 实体相似度阈值 |
| max_entities | 25 | 最大实体数量 |
| entity_weight_threshold | 0.05 | 实体权重阈值 |
| final_entity_count | 15 | 最终返回实体数量 |

### ExpandConfig（扩展配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| enabled | True | 是否启用扩展 |
| max_hops | 3 | 最大跳数 |
| entities_per_hop | 10 | 每跳新增实体数 |
| weight_change_threshold | 0.1 | 收敛阈值 |

### RerankConfig（重排配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| strategy | PAGERANK | 排序策略（PAGERANK/RRF） |
| score_threshold | 0.5 | 分数阈值 |
| max_results | 10 | 最大返回数量 |
| pagerank_section_top_k | 15 | PageRank段落检索数量 |
| rrf_k | 60 | RRF融合参数K |

## 📝 线索结构

每条线索包含：

```python
{
    "id": str,          # 线索ID
    "stage": str,       # 阶段（recall/expand/rerank）
    "from": {           # 起点节点
        "id": str,
        "type": str,    # query/entity/event
        "category": str,
        "content": str,
        "description": str
    },
    "to": {...},        # 终点节点（同上）
    "confidence": float,  # 置信度 [0.0, 1.0]
    "relation": str,    # 关系类型
    "metadata": dict    # 元数据
}
```

## 🔧 开发指南

### 添加新的排序策略

1. 在 `ranking/` 目录创建新文件
2. 继承基类或实现相同接口
3. 在 `ranking/__init__.py` 导出
4. 在 `searcher.py` 中注册

### 调试技巧

开启详细日志：

```python
import logging
logging.getLogger("search").setLevel(logging.DEBUG)
```

查看线索统计：

```python
result = await searcher.search(config)
print(f"Recall线索: {len([c for c in result['clues'] if c['stage'] == 'recall'])}")
print(f"Expand线索: {len([c for c in result['clues'] if c['stage'] == 'expand'])}")
print(f"Rerank线索: {len([c for c in result['clues'] if c['stage'] == 'rerank'])}")
```

## 📚 相关文档

- [算法原理](../../docs/search/base.md)
- [API文档](../../docs/api/README.md)
- [部署指南](../../docs/deploy/README.md)

## 🎯 性能指标

- **召回时间**: 1-2秒
- **扩展时间**: 0.5-1.5秒
- **重排时间**: 
  - PageRank: 1-3秒
  - RRF: 0.3-0.8秒
- **总耗时**: 2-5秒

## ✨ 重构亮点

1. **具象化命名** - 不再使用stage1/2/3，改用recall/expand/rerank
2. **简化架构** - 移除LLM和RAG处理器，只保留SAG
3. **分层配置** - 清晰的三阶段配置结构
4. **完整线索** - 支持前端图谱可视化
5. **易于调适** - 参数清晰，便于优化

---

**最后更新**: 2025-11-04  
**版本**: v2.0  
**维护者**: SAG Team

