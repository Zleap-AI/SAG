/**
 * localStorage utility for persisting search history
 */

const STORAGE_KEY = 'search-history';
const MAX_HISTORY_ITEMS = 20; // 最多保存20条历史记录

export interface SearchHistoryItem {
  id: string;                    // 唯一标识
  query: string;                 // 搜索查询文本
  plainQuery: string;            // 纯文本查询（去除 @ 标记）
  sourceId?: string;             // 信息源 ID
  sourceName?: string;           // 信息源名称
  mode: 'fast' | 'normal';      // 搜索模式
  timestamp: number;             // 时间戳
}

/**
 * Generate unique ID for history item
 */
function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * Save a new search query to history
 * Automatically deduplicates and limits the number of items
 */
export function saveSearchHistory(item: Omit<SearchHistoryItem, 'id' | 'timestamp'>): void {
  try {
    const history = loadSearchHistory();

    // 去重：移除相同的查询（基于 plainQuery 和 sourceId）
    const filteredHistory = history.filter(
      h => !(h.plainQuery === item.plainQuery && h.sourceId === item.sourceId)
    );

    // 添加新记录到开头
    const newItem: SearchHistoryItem = {
      ...item,
      id: generateId(),
      timestamp: Date.now(),
    };

    const updatedHistory = [newItem, ...filteredHistory];

    // 限制数量
    const limitedHistory = updatedHistory.slice(0, MAX_HISTORY_ITEMS);

    localStorage.setItem(STORAGE_KEY, JSON.stringify(limitedHistory));
  } catch (error) {
    console.error('Failed to save search history:', error);
  }
}

/**
 * Load search history from localStorage
 * Returns empty array if no history exists
 */
export function loadSearchHistory(): SearchHistoryItem[] {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return [];

    const history = JSON.parse(saved) as SearchHistoryItem[];

    // 按时间倒序排列（最新的在前）
    return history.sort((a, b) => b.timestamp - a.timestamp);
  } catch (error) {
    console.error('Failed to load search history:', error);
    return [];
  }
}

/**
 * Delete a specific history item by ID
 */
export function deleteSearchHistoryItem(id: string): void {
  try {
    const history = loadSearchHistory();
    const updatedHistory = history.filter(item => item.id !== id);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(updatedHistory));
  } catch (error) {
    console.error('Failed to delete history item:', error);
  }
}

/**
 * Clear all search history from localStorage
 */
export function clearSearchHistory(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (error) {
    console.error('Failed to clear search history:', error);
  }
}

/**
 * Check if there is any search history
 */
export function hasSearchHistory(): boolean {
  try {
    const history = loadSearchHistory();
    return history.length > 0;
  } catch (error) {
    return false;
  }
}

/**
 * Format timestamp to readable time label
 */
export function formatTimeLabel(timestamp: number): string {
  const now = Date.now();
  const diff = now - timestamp;

  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;

  if (diff < minute) {
    return '刚才';
  } else if (diff < hour) {
    const minutes = Math.floor(diff / minute);
    return `${minutes}分钟前`;
  } else if (diff < day) {
    const hours = Math.floor(diff / hour);
    return `${hours}小时前`;
  } else if (diff < 2 * day) {
    return '昨天';
  } else if (diff < 7 * day) {
    const days = Math.floor(diff / day);
    return `${days}天前`;
  } else {
    return new Date(timestamp).toLocaleDateString('zh-CN', {
      month: 'numeric',
      day: 'numeric',
    });
  }
}
