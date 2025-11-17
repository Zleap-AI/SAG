/**
 * localStorage utility for persisting search parameters
 */

const STORAGE_KEY = 'search-params';

export interface SearchParams {
  // Basic parameters (Rerank)
  max_results?: number;
  score_threshold?: number;

  // Stage 1: Recall (Key/Entity and Event discovery)
  entity_similarity_threshold?: number;
  event_similarity_threshold?: number;
  max_entities?: number;
  max_events?: number;
  entity_weight_threshold?: number;
  final_entity_count?: number;
  vector_top_k?: number;
  vector_candidates?: number;

  // Stage 2: Expand (Multi-hop search)
  expand_enabled?: boolean;
  max_hops?: number;
  entities_per_hop?: number;
  expand_event_similarity_threshold?: number;
  weight_change_threshold?: number;
  min_events_per_hop?: number;
  max_events_per_hop?: number;

  // Stage 3: Rerank (Paragraph retrieval and ranking)
  use_pagerank?: boolean;  // 是否使用PageRank策略
  max_key_recall_results?: number;  // Step1 Key召回的最大事项/段落数
  max_query_recall_results?: number;  // Step2 Query召回的最大事项/段落数
  pagerank_damping_factor?: number;  // PageRank阻尼系数
  pagerank_max_iterations?: number;  // PageRank最大迭代次数
  rrf_k?: number;  // RRF融合参数K

  // Allow any additional parameters
  [key: string]: any;
}

/**
 * Save search parameters to localStorage
 */
export function saveSearchParams(params: SearchParams): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(params));
  } catch (error) {
    console.error('Failed to save search parameters:', error);
    throw new Error('Failed to save settings');
  }
}

/**
 * 参数名迁移映射表（旧名 -> 新名）
 */
const PARAM_MIGRATION_MAP: Record<string, string> = {
  // Basic
  'top_k': 'max_results',
  'threshold': 'score_threshold',
  'similarity_threshold': 'score_threshold',

  // Stage1 (Recall)
  'key_similarity_threshold': 'entity_similarity_threshold',
  'max_keys': 'max_entities',
  'final_key_threshold': 'entity_weight_threshold',
  'top_n_keys': 'final_entity_count',
  'vector_k': 'vector_top_k',
  'vector_num_candidates': 'vector_candidates',

  // Stage2 (Expand)
  'enable_stage2': 'expand_enabled',
  'max_jumps': 'max_hops',
  'topkey': 'entities_per_hop',
  'stage2_event_threshold': 'expand_event_similarity_threshold',
  'stage2_convergence_threshold': 'weight_change_threshold',
  'stage2_min_events': 'min_events_per_hop',
  'stage2_max_events': 'max_events_per_hop',

  // Stage3 - 旧的 stage3 参数不再使用，移除
  'stage3_vector_k': '__removed__',
  'stage3_top_n_page': '__removed__',
  'stage3_top_n_event': '__removed__',
  'stage3_embedding_threshold': '__removed__',
  'stage3_event_threshold': '__removed__',
  'pagerank_section_top_k': '__removed__',
};

/**
 * Load search parameters from localStorage
 * Returns null if no saved parameters exist
 */
export function loadSearchParams(): SearchParams | null {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return null;

    const oldParams = JSON.parse(saved) as SearchParams;
    const migratedParams: SearchParams = {};

    // 迁移旧参数名到新参数名
    for (const [oldKey, value] of Object.entries(oldParams)) {
      const newKey = PARAM_MIGRATION_MAP[oldKey];

      if (newKey === '__removed__') {
        // 该参数已被移除，跳过
        continue;
      } else if (newKey) {
        // 需要迁移的参数
        migratedParams[newKey] = value;
      } else {
        // 参数名未变，直接保留
        migratedParams[oldKey] = value;
      }
    }

    // 🆕 与默认值合并，确保新参数有默认值
    // 注意：需要在运行时动态导入，避免循环依赖
    const { getDefaultSearchParams } = require('./search-config');
    const defaults = getDefaultSearchParams();

    return { ...defaults, ...migratedParams };
  } catch (error) {
    console.error('Failed to load search parameters:', error);
    return null;
  }
}

/**
 * Clear saved search parameters from localStorage
 */
export function clearSearchParams(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (error) {
    console.error('Failed to clear search parameters:', error);
    throw new Error('Failed to clear settings');
  }
}

/**
 * Check if there are saved parameters in localStorage
 */
export function hasSavedParams(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) !== null;
  } catch (error) {
    return false;
  }
}

/**
 * Compare two parameter objects to check if they're equal
 */
export function areParamsEqual(params1: SearchParams, params2: SearchParams): boolean {
  return JSON.stringify(params1) === JSON.stringify(params2);
}
