// 通用类型
export interface ApiResponse<T> {
  success: boolean
  data: T
  message?: string
}

export interface PaginatedResponse<T> {
  success: boolean
  data: T[]
  pagination: {
    total: number
    page: number
    page_size: number
    total_pages: number
  }
}

export interface ErrorResponse {
  success: false
  error: {
    code: string
    message: string
    details?: any
  }
}

// 信息源
export interface Source {
  id: string
  name: string
  description?: string
  config?: any
  created_time: string
  updated_time?: string
  document_count: number
  entity_types_count: number
}

// 🆕 值约束配置
export interface ValueConstraints {
  type: 'int' | 'float' | 'datetime' | 'bool' | 'enum' | 'text'
  enum_values?: string[]  // 枚举类型的可选值列表
  min?: number            // 数值类型的最小值
  max?: number            // 数值类型的最大值
  unit?: string           // 数值类型的单位（如 "元", "美元", "kg"）
  default?: any           // 默认值（类型取决于 type 字段）
  override?: boolean      // 强制模式：true 时覆盖 LLM 提取结果，始终使用默认值
}

// 实体类型
export interface EntityType {
  id: string
  scope?: 'global' | 'source' | 'article'  // 🆕 应用范围
  source_config_id?: string
  article_id?: string  // 🆕 文档ID
  type: string
  name: string
  description?: string
  weight: number
  similarity_threshold: number
  is_active: boolean
  is_default: boolean
  extra_data?: any
  // 🆕 值类型化配置字段
  value_format?: string
  value_constraints?: ValueConstraints
  created_time?: string
  updated_time?: string
  // 🆕 用于显示的额外字段（从后端返回）
  _sourceName?: string
  _articleTitle?: string
}

// 文档
export interface Document {
  id: string
  source_config_id: string
  title: string
  summary?: string
  category?: string
  tags?: string[]
  status: 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED'
  extra_data?: any
  created_time: string
  updated_time?: string
  sections_count: number
  events_count: number
}

// 文章片段
export interface ArticleSection {
  id: string
  article_id: string
  rank: number
  heading: string
  content: string
  extra_data?: {
    type?: string
    length?: number
    [key: string]: any
  }
  created_time: string
  updated_time: string
}

// 实体信息
export interface EntityInfo {
  id: string
  name: string
  type: string
  weight: number
  description?: string  // 该实体在此事项中的具体描述/角色
}

// 事项
export interface SourceEvent {
  id: string
  source_config_id: string
  article_id: string
  title: string
  summary: string
  content: string
  rank: number
  start_time?: string
  end_time?: string
  references?: ArticleSection[]
  entities?: EntityInfo[]
  extra_data?: {
    category?: string
    priority?: string
    tags?: string[]
    [key: string]: any
  }
  created_time: string
  updated_time: string
}


// 任务
export interface Task {
  task_id: string
  task_type?: string  // 任务类型：document_upload, pipeline_run
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled'
  progress?: number
  message?: string
  result?: any
  error?: string
  created_time?: string
  updated_time?: string
  // 关联信息
  source_config_id?: string
  source_name?: string
  article_id?: string
  article_title?: string
}

// 任务统计
export interface TaskStats {
  total: number
  by_status: {
    pending: number
    processing: number
    completed: number
    failed: number
    cancelled: number
  }
  by_type: {
    document_upload: number
    pipeline_run: number
  }
}

// 搜索结果
export interface SearchResult {
  id: string
  title: string
  summary: string
  content: string
  score: number
  [key: string]: any
}

// 搜索响应
export interface SearchResponse {
  success: boolean
  data: {
    events: SearchResult[]
    total: number
  }
  query: string
  mode: 'llm' | 'rag' | 'sag'
  execution_time?: number
}

// 流程配置
export interface PipelineConfig {
  source_config_id: string
  task_name?: string
  background?: string
  load?: {
    path: string
    recursive?: boolean
    pattern?: string
  }
  extract?: {
    parallel?: boolean
    max_concurrency?: number
  }
  search?: {
    query: string
    mode?: 'llm' | 'rag' | 'sag'
    top_k?: number
    threshold?: number
  }
  output?: {
    mode?: 'full' | 'id_only'
    format?: 'json' | 'markdown'
  }
}

// 模型配置（LLM、Embedding等）
export interface ModelConfig {
  id: string
  name: string
  description?: string
  
  // 双维度分类
  type: 'llm' | 'embedding' | 'rerank'  // 模型类型
  scenario: 'extract' | 'search' | 'chat' | 'summary' | 'general'  // 使用场景
  
  // API配置
  provider?: string
  api_key: string
  base_url: string
  model: string
  
  // LLM专用参数（embedding等类型不使用）
  temperature: number
  max_tokens: number
  top_p: number
  frequency_penalty: number
  presence_penalty: number
  
  // 通用参数
  timeout: number
  max_retries: number
  
  // 扩展数据（模型特定，如embedding的dimensions）
  extra_data?: {
    dimensions?: number
    [key: string]: any
  }
  
  // 状态和优先级
  is_active: boolean
  priority: number
  
  created_time: string
  updated_time: string
  created_by?: string
}

