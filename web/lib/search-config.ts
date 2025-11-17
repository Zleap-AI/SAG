/**
 * 搜索参数配置
 *
 * 定义所有搜索参数的元数据，用于动态生成UI
 */

export interface SearchParam {
  key: string
  label: string
  desc: string
  type?: 'number' | 'boolean' | 'select'
  min?: number
  max?: number
  step?: number
  options?: Array<{ value: string; label: string }>  // for select type
  default: number | boolean
}

export interface SearchParamGroup {
  key: string
  label: string
  icon: string
  params: SearchParam[]
}

/**
 * 所有搜索参数配置
 * 按照 Stage 分组，与 sag/modules/search/config.py 保持一致
 */
export const SEARCH_PARAM_GROUPS: SearchParamGroup[] = [
  {
    key: 'basic',
    label: '基础参数',
    icon: 'Target',
    params: [
      {
        key: 'max_results',
        label: '结果数量',
        desc: '返回数量上限',
        type: 'number',
        min: 1,
        max: 100,
        step: 1,
        default: 10,
      },
      {
        key: 'score_threshold',
        label: '相似度阈值',
        desc: '相关度阈值，过滤低相关结果',
        type: 'number',
        min: 0,
        max: 1,
        step: 0.05,
        default: 0.5,
      },
    ],
  },
  {
    key: 'stage1',
    label: 'Recall - 实体召回',
    icon: 'Search',
    params: [
      {
        key: 'entity_similarity_threshold',
        label: 'Key相似度阈值',
        desc: 'Key语义相似度阈值，控制候选Key的召回',
        type: 'number',
        min: 0,
        max: 1,
        step: 0.05,
        default: 0.4,
      },
      {
        key: 'event_similarity_threshold',
        label: 'Event相似度阈值',
        desc: 'Event语义相似度阈值，控制候选事项的召回',
        type: 'number',
        min: 0,
        max: 1,
        step: 0.05,
        default: 0.4,
      },
      {
        key: 'max_entities',
        label: '最大Key数量',
        desc: '最多发现的Key实体数量',
        type: 'number',
        min: 1,
        max: 100,
        step: 1,
        default: 25,
      },
      {
        key: 'max_events',
        label: '最大Event数量',
        desc: '最多发现的Event事项数量',
        type: 'number',
        min: 1,
        max: 200,
        step: 1,
        default: 60,
      },
      {
        key: 'entity_weight_threshold',
        label: '最终Key权重阈值',
        desc: '最终Key筛选的权重阈值',
        type: 'number',
        min: 0,
        max: 1,
        step: 0.05,
        default: 0.05,
      },
      {
        key: 'final_entity_count',
        label: 'Top-N Key数量',
        desc: '返回Top-N个重要Key',
        type: 'number',
        min: 1,
        max: 50,
        step: 1,
        default: 15,
      },
      {
        key: 'vector_top_k',
        label: '向量搜索K值',
        desc: '向量搜索返回的结果数量',
        type: 'number',
        min: 1,
        max: 50,
        step: 1,
        default: 15,
      },
      {
        key: 'vector_candidates',
        label: '向量候选数量',
        desc: '向量搜索的候选数量（ANN算法参数）',
        type: 'number',
        min: 10,
        max: 200,
        step: 10,
        default: 100,
      },
    ],
  },
  {
    key: 'stage2',
    label: 'Expand - 实体扩展',
    icon: 'GitBranch',
    params: [
      {
        key: 'expand_enabled',
        label: '启用Expand',
        desc: '是否启用实体扩展算法',
        type: 'boolean',
        default: true,
      },
      {
        key: 'max_hops',
        label: '最大跳跃次数',
        desc: '多跳搜索的最大跳跃次数',
        type: 'number',
        min: 1,
        max: 10,
        step: 1,
        default: 3,
      },
      {
        key: 'entities_per_hop',
        label: '每跳最大Key数',
        desc: '每一跳的最大Key数量',
        type: 'number',
        min: 1,
        max: 50,
        step: 1,
        default: 10,
      },
      {
        key: 'expand_event_similarity_threshold',
        label: 'Event相似度阈值',
        desc: 'Stage2 Event相似度阈值',
        type: 'number',
        min: 0,
        max: 1,
        step: 0.05,
        default: 0.3,
      },
      {
        key: 'weight_change_threshold',
        label: '收敛阈值',
        desc: '权重变化小于此值时停止多跳',
        type: 'number',
        min: 0,
        max: 1,
        step: 0.05,
        default: 0.1,
      },
      {
        key: 'min_events_per_hop',
        label: '最少Event数',
        desc: '每轮最少的Event数量',
        type: 'number',
        min: 1,
        max: 50,
        step: 1,
        default: 5,
      },
      {
        key: 'max_events_per_hop',
        label: '最多Event数',
        desc: '每轮最多的Event数量',
        type: 'number',
        min: 1,
        max: 500,
        step: 10,
        default: 100,
      },
    ],
  },
  {
    key: 'stage3',
    label: 'Rerank - 事项重排',
    icon: 'BarChart3',
    params: [
      {
        key: 'use_pagerank',
        label: '启用PageRank',
        desc: '是否使用PageRank算法排序（关闭则使用RRF算法）',
        type: 'boolean',
        default: true,
      },
      {
        key: 'max_key_recall_results',
        label: 'Step1 Key召回数量',
        desc: 'Step1 Key召回的最大事项/段落数（按相似度排序截断）',
        type: 'number',
        min: 5,
        max: 200,
        step: 5,
        default: 30,
      },
      {
        key: 'max_query_recall_results',
        label: 'Step2 Query召回数量',
        desc: 'Step2 Query召回的最大事项/段落数（按相似度排序截断）',
        type: 'number',
        min: 5,
        max: 200,
        step: 5,
        default: 30,
      },
      {
        key: 'pagerank_damping_factor',
        label: 'PageRank阻尼系数',
        desc: 'PageRank算法的阻尼系数，控制随机跳转概率',
        type: 'number',
        min: 0,
        max: 1,
        step: 0.05,
        default: 0.85,
      },
      {
        key: 'pagerank_max_iterations',
        label: 'PageRank最大迭代次数',
        desc: 'PageRank算法的最大迭代次数',
        type: 'number',
        min: 1,
        max: 1000,
        step: 10,
        default: 100,
      },
      {
        key: 'rrf_k',
        label: 'RRF融合参数K',
        desc: 'RRF算法的融合参数K值',
        type: 'number',
        min: 1,
        max: 100,
        step: 1,
        default: 60,
      },
    ],
  },
]

/**
 * 获取所有参数的默认值
 */
export function getDefaultSearchParams(): Record<string, number | boolean> {
  const defaults: Record<string, number | boolean> = {}

  SEARCH_PARAM_GROUPS.forEach(group => {
    group.params.forEach(param => {
      defaults[param.key] = param.default
    })
  })

  return defaults
}

/**
 * 获取指定分组的参数
 */
export function getParamsByGroup(groupKey: string): SearchParam[] {
  const group = SEARCH_PARAM_GROUPS.find(g => g.key === groupKey)
  return group?.params || []
}
