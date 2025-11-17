/**
 * 搜索响应类型定义
 *
 * 对应后端 sag/modules/search 模块的返回数据结构
 */

// ==================== 节点类型 ====================

/**
 * 节点统一格式（5个字段）
 */
export interface Node {
  /** 节点唯一ID（阶段隔离，如 recall_{uuid}, expand_hop1_{uuid}） */
  id: string;
  /** 节点类型 */
  type: 'query' | 'entity' | 'event' | 'section';
  /** 节点类别（query: origin/rewrite, entity: person/topic/location等） */
  category: string;
  /** 节点内容（显示文本） */
  content: string;
  /** 节点描述 */
  description: string;
  /** 跳数（用于显示标签）：hop=0 (Recall), hop=1 (第1跳), hop=2 (第2跳)... */
  hop?: number;
  /** 数据库原始ID（仅 event 类型，用于前端查询详情） */
  event_id?: string;
  /** 阶段标识（用于在节点右上角显示标签，主要用于 event 和 entity 类型） */
  stage?: 'recall' | 'expand' | 'rerank';
}

// ==================== 线索类型 ====================

/**
 * 线索元数据
 */
export interface ClueMetadata {
  /** 方法（vector_search/database_lookup/cooccurrence等） */
  method?: string;
  /** 步骤（step1/step2/step3等） */
  step?: string;
  /** 跳数（仅expand阶段） */
  hop?: number;
  /** 相似度（仅向量搜索） */
  similarity?: number;
  /** 实体权重 */
  entity_weight?: number;
  /** RRF分数 */
  rrf_score?: number;
  /** PageRank分数 */
  pagerank_score?: number;
  /** BM25分数 */
  bm25_score?: number;
  /** Embedding排名 */
  embedding_rank?: number;
  /** BM25排名 */
  bm25_rank?: number;
  /** 其他自定义字段 */
  [key: string]: any;
}

/**
 * 线索对象（知识图谱的边）
 */
export interface Clue {
  /** 线索唯一ID（UUID4） */
  id: string;
  /** 阶段标识 */
  stage: 'prepare' | 'recall' | 'expand' | 'rerank';
  /** 起点节点 */
  from: Node;
  /** 终点节点 */
  to: Node;
  /** 置信度 [0, 1] */
  confidence: number;
  /** 关系类型 */
  relation: string;
  /** 元数据 */
  metadata: ClueMetadata;
  /** 显示级别（final/intermediate/debug） */
  display_level?: 'final' | 'intermediate' | 'debug';
}

// ==================== 事项类型 ====================

/**
 * 事项对象
 */
export interface SourceEvent {
  /** UUID */
  id: string;
  /** 数据源ID */
  source_config_id: string;
  /** 文章ID */
  article_id: string;
  /** 事项标题 */
  title: string;
  /** 事项摘要 */
  summary: string;
  /** 事项内容 */
  content: string;
  /** 事项分类（技术/产品/市场/研究/管理等） */
  category?: string;
  /** 排序序号 */
  rank: number;
  /** 开始时间（ISO格式） */
  start_time?: string;
  /** 结束时间 */
  end_time?: string;
  /** 原始片段引用 */
  references?: Record<string, any>;
  /** 扩展数据 */
  extra_data?: Record<string, any>;
  /** 创建时间 */
  created_time: string;
  /** 更新时间 */
  updated_time: string;
}

// ==================== 统计信息类型 ====================

/**
 * Recall阶段统计
 */
export interface RecallStats {
  /** 召回实体数量 */
  entities_count: number;
  /** 按类型统计 */
  by_type: Record<string, number>;
}

/**
 * Expand阶段统计
 */
export interface ExpandStats {
  /** 扩展发现的新实体数 */
  entities_count: number;
  /** 总实体数（包括recall） */
  total_entities: number;
  /** 实际跳跃次数 */
  hops: number;
  /** 是否收敛 */
  converged: boolean;
}

/**
 * Rerank阶段统计
 */
export interface RerankStats {
  /** 最终事项数量 */
  events_count: number;
  /** 排序策略 */
  strategy: 'pagerank' | 'rrf';
}

/**
 * 搜索统计信息
 */
export interface SearchStats {
  recall: RecallStats;
  expand: ExpandStats;
  rerank: RerankStats;
}

// ==================== 查询信息类型 ====================

/**
 * 查询信息
 */
export interface QueryInfo {
  /** 原始查询 */
  original: string;
  /** 当前查询（可能被重写） */
  current: string;
  /** 是否发生重写 */
  rewritten: boolean;
}

// ==================== 搜索响应类型 ====================

/**
 * 搜索API完整响应
 */
export interface SearchResponse {
  /** 最终事项列表 */
  events: SourceEvent[];
  /** 完整线索链（用于知识图谱） */
  clues: Clue[];
  /** 三阶段统计信息 */
  stats: SearchStats;
  /** 查询信息 */
  query: QueryInfo;
}

// ==================== 图谱数据类型 ====================

/**
 * Relation-Graph 节点格式
 */
export interface GraphNode {
  id: string;
  text: string;
  html?: string;  // HTML 模板内容
  type?: string;
  color?: string;
  fontColor?: string;
  fontSize?: number;
  width?: number;
  height?: number;
  borderWidth?: number;
  borderColor?: string;
  data?: Node;
}

/**
 * Relation-Graph 连线格式
 */
export interface GraphLine {
  from: string;
  to: string;
  text?: string;
  color?: string;
  lineWidth?: number;
  fontColor?: string;
  data?: Clue;
}

/**
 * Relation-Graph 数据格式
 */
export interface GraphData {
  rootId: string;
  nodes: GraphNode[];
  lines: GraphLine[];
}

// ==================== 图谱统计类型 ====================

/**
 * 图谱统计信息
 */
export interface GraphStats {
  /** 总节点数 */
  totalNodes: number;
  /** 按类型统计节点 */
  nodesByType: Record<string, number>;
  /** 总连线数 */
  totalLines: number;
  /** 按阶段统计连线 */
  linesByStage: Record<string, number>;
  /** 平均置信度 */
  avgConfidence: number;
}

// ==================== 查询分析类型 ====================

/**
 * 查询提取的实体
 */
export interface QueryEntity {
  id: string;
  name: string;
  type: string;
  weight: number;
}
