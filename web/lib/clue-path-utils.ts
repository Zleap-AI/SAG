/**
 * 线索路径反推工具函数
 *
 * 根据选中的阶段组合，智能反推完整推理路径
 *
 * 阶段优先级：Rerank > Expand > Recall
 *
 * 反推规则：
 * - 选中 [Recall]：反推 Recall final → query
 * - 选中 [Recall, Expand]：反推 Expand final → query
 * - 选中 [Recall, Expand, Rerank]：反推 Rerank final → query
 * - 选中 [Expand]：反推 Expand final → Recall final（不到 query）
 * - 选中 [Rerank]：反推 Rerank final → Expand final（不到 query）
 * - 选中 [Expand, Rerank]：反推 Rerank final → Recall final（跳过 Recall）
 *
 * 🆕 多路径支持：
 * - 使用 DFS 算法查找所有可能的推理路径
 * - 当一个节点有多个父节点时，会展开所有路径
 * - 支持路径数量限制，防止路径爆炸（默认 500 条）
 * - 按置信度排序路径，优先展示高置信度路径
 * - 路径隔离去重：确保每条线索都真正属于有效路径
 */

import type { Clue, Node } from "@/types/search-response";

/**
 * 阶段优先级映射
 */
const STAGE_PRIORITY: Record<string, number> = {
  'prepare': 0,
  'recall': 1,
  'expand': 2,
  'rerank': 3,
};

/**
 * 获取选中阶段中优先级最高的阶段
 */
function getHighestStage(selectedStages: string[]): string | null {
  if (selectedStages.length === 0) return null;

  let highest = selectedStages[0];
  let highestPriority = STAGE_PRIORITY[highest] || 0;

  selectedStages.forEach(stage => {
    const priority = STAGE_PRIORITY[stage] || 0;
    if (priority > highestPriority) {
      highest = stage;
      highestPriority = priority;
    }
  });

  return highest;
}

/**
 * 获取前一个阶段
 */
function getPreviousStage(stage: string): string | null {
  const priority = STAGE_PRIORITY[stage];
  if (priority === undefined || priority <= 0) return null;

  // 找到优先级为 priority - 1 的阶段
  for (const [s, p] of Object.entries(STAGE_PRIORITY)) {
    if (p === priority - 1) return s;
  }
  return null;
}

/**
 * 从最终线索反推到指定终点的所有完整路径（DFS 查找）
 *
 * @param finalClue - 最终线索（display_level="final"）
 * @param allClues - 所有线索数据
 * @param stopAtStage - 停止阶段（如果指定，反推到该阶段的 final 节点就停止）
 * @param maxPaths - 最大路径数限制，防止路径爆炸（默认 500）
 * @returns 所有完整的推理路径
 */
export function findAllCompletePaths(
  finalClue: Clue,
  allClues: Clue[],
  stopAtStage?: string | null,
  maxPaths: number = 500
): Clue[][] {
  const allPaths: Clue[][] = [];

  /**
   * 检查节点是否是停止阶段的 final 节点
   */
  function isStopNode(node: Node): boolean {
    if (!stopAtStage) return false;

    return allClues.some(clue =>
      clue.to.id === node.id &&
      clue.stage === stopAtStage &&
      clue.display_level === 'final'
    );
  }

  /**
   * DFS 递归查找所有路径
   */
  function dfs(
    currentPath: Clue[],
    currentNode: Node,
    visited: Set<string>
  ): void {
    // 限制路径数量，防止爆炸
    if (allPaths.length >= maxPaths) {
      return;
    }

    // 停止条件1：到达 query 节点
    if (currentNode.type === 'query') {
      allPaths.push([...currentPath]);
      return;
    }

    // 停止条件2：到达停止阶段的 final 节点
    if (isStopNode(currentNode)) {
      // 找到停止阶段的 final 线索，添加到路径中
      const stopClue = allClues.find(clue =>
        clue.to.id === currentNode.id &&
        clue.stage === stopAtStage &&
        clue.display_level === 'final'
      );

      if (stopClue && !visited.has(stopClue.to.id)) {
        allPaths.push([stopClue, ...currentPath]);
      } else {
        allPaths.push([...currentPath]);
      }
      return;
    }

    // 停止条件3：检测到循环
    if (visited.has(currentNode.id)) {
      console.warn('检测到循环路径，停止该分支', currentNode.id);
      return;
    }

    // 标记已访问
    const newVisited = new Set(visited);
    newVisited.add(currentNode.id);

    // 🆕 查找所有父线索（不只是第一个）
    const parentClues = allClues.filter(clue =>
      clue.to.id === currentNode.id &&
      (clue.display_level === 'intermediate' || clue.display_level === 'final')
    );

    // 如果没有父线索，路径断开，保存当前路径
    if (parentClues.length === 0) {
      console.warn('路径断开：找不到节点的父线索', currentNode);
      allPaths.push([...currentPath]);
      return;
    }

    // 按置信度排序（优先展示高置信度路径）
    const sortedParentClues = parentClues.sort((a, b) =>
      (b.confidence || 0) - (a.confidence || 0)
    );

    // 递归查找每个父线索的路径
    for (const parentClue of sortedParentClues) {
      // 检查路径数限制
      if (allPaths.length >= maxPaths) {
        console.warn(`路径数量达到限制 (${maxPaths})，停止搜索`);
        break;
      }

      dfs(
        [parentClue, ...currentPath],
        parentClue.from,
        newVisited
      );
    }
  }

  // 从 final 线索开始 DFS
  dfs([finalClue], finalClue.from, new Set([finalClue.to.id]));

  return allPaths;
}

/**
 * 从最终线索反推到指定终点的完整路径（单路径版本）
 *
 * @param finalClue - 最终线索（display_level="final"）
 * @param allClues - 所有线索数据
 * @param stopAtStage - 停止阶段（如果指定，反推到该阶段的 final 节点就停止）
 * @returns 完整的推理路径
 * @deprecated 推荐使用 findAllCompletePaths 获取所有路径
 */
export function findCompletePath(
  finalClue: Clue,
  allClues: Clue[],
  stopAtStage?: string | null
): Clue[] {
  const path: Clue[] = [finalClue];
  let currentNode = finalClue.from;
  const visitedNodes = new Set<string>([finalClue.to.id]);  // 防止循环

  // 一直往回找，直到满足停止条件
  while (true) {
    // 停止条件1：到达 query 节点
    if (currentNode.type === 'query') {
      break;
    }

    // 停止条件2：如果指定了 stopAtStage，检查当前节点是否是该阶段的 final 节点
    if (stopAtStage) {
      const isFinalNodeOfStopStage = allClues.some(clue =>
        clue.to.id === currentNode.id &&
        clue.stage === stopAtStage &&
        clue.display_level === 'final'
      );

      if (isFinalNodeOfStopStage) {
        // 找到停止阶段的 final 线索，添加到路径中
        const stopClue = allClues.find(clue =>
          clue.to.id === currentNode.id &&
          clue.stage === stopAtStage &&
          clue.display_level === 'final'
        );
        if (stopClue && !visitedNodes.has(stopClue.to.id)) {
          path.unshift(stopClue);
        }
        break;
      }
    }

    // 防止无限循环
    if (visitedNodes.has(currentNode.id)) {
      console.warn('检测到循环路径，停止反推', currentNode.id);
      break;
    }
    visitedNodes.add(currentNode.id);

    // 查找父线索：to.id === currentNode.id
    const parentClue = allClues.find(clue =>
      clue.to.id === currentNode.id &&
      (clue.display_level === 'intermediate' || clue.display_level === 'final')
    );

    if (parentClue) {
      path.unshift(parentClue);  // 添加到路径前面
      currentNode = parentClue.from;
    } else {
      // 找不到父节点，路径断开
      console.warn('路径断开：找不到节点的父线索', currentNode);
      break;
    }
  }

  return path;
}

/**
 * 批量反推：为所有最终线索生成完整路径（支持多路径）
 *
 * 🔧 路径隔离去重：确保每条线索都真正属于有效路径
 *
 * @param clues - 所有线索
 * @param selectedStages - 选中的阶段列表
 * @returns 扩展后的线索列表（只包含有效路径上的线索）
 */
export function expandFinalClues(
  clues: Clue[],
  selectedStages: string[]
): Clue[] {
  // 1. 确定最高阶段（优先级最高的选中阶段）
  const highestStage = getHighestStage(selectedStages);
  if (!highestStage) {
    return [];
  }

  // 2. 确定反推的终点
  // - 如果最高阶段的前一阶段没有被选中，则反推到前一阶段的 final 节点
  // - 否则反推到 query
  const previousStage = getPreviousStage(highestStage);
  const shouldStopAtPreviousStage = previousStage && !selectedStages.includes(previousStage);
  const stopAtStage = shouldStopAtPreviousStage ? previousStage : null;

  console.log('精简模式配置:', {
    selectedStages,
    highestStage,
    previousStage,
    stopAtStage,
    shouldStopAtPreviousStage
  });

  // 3. 🆕 根据最高阶段决定要显示哪些阶段的 final 线索
  let finalClues: Clue[];

  if (highestStage === 'rerank') {
    // rerank 阶段：只显示 rerank 的 final
    finalClues = clues.filter(c =>
      c.display_level === 'final' &&
      c.stage === 'rerank'
    );
    console.log(`找到 ${finalClues.length} 条 rerank 阶段的 final 线索`);
  } else if (highestStage === 'expand') {
    // expand 阶段：显示 expand + recall 的 final
    finalClues = clues.filter(c =>
      c.display_level === 'final' &&
      (c.stage === 'expand' || c.stage === 'recall')
    );
    console.log(`找到 ${finalClues.length} 条 expand+recall 阶段的 final 线索`);
  } else {
    // recall 或其他阶段：只显示该阶段的 final
    finalClues = clues.filter(c =>
      c.display_level === 'final' &&
      c.stage === highestStage
    );
    console.log(`找到 ${finalClues.length} 条 ${highestStage} 阶段的 final 线索`);
  }

  // 🆕 兼容性检查：如果该阶段没有 final 线索，返回空数组（清空图谱）
  if (finalClues.length === 0) {
    console.warn(`⚠️ 精简模式: ${highestStage} 阶段没有 final 线索，图谱将为空`);
    console.warn(`💡 提示: 该阶段可能还未实现 final 标记，或者没有最终结果`);
    return [];
  }

  // 4. 🔧 为每条 final 线索反推所有完整路径，并标记路径归属
  interface ClueWithPaths {
    clue: Clue;
    pathIndices: Set<number>;  // 该线索属于哪些路径（全局路径索引）
  }

  const cluePathMap = new Map<string, ClueWithPaths>();  // clue.id -> ClueWithPaths
  let globalPathIndex = 0;
  let totalPaths = 0;

  finalClues.forEach(finalClue => {
    // 🆕 使用新的 findAllCompletePaths 查找所有路径
    const allPaths = findAllCompletePaths(finalClue, clues, stopAtStage);
    totalPaths += allPaths.length;

    console.log(
      `反推 final 线索 ${finalClue.to.id.substring(0, 8)}... ` +
      `找到 ${allPaths.length} 条路径`
    );

    // 🔧 为每条路径分配全局索引，并记录每条线索属于哪些路径
    allPaths.forEach((path, idx) => {
      const currentPathIndex = globalPathIndex++;

      console.log(
        `  路径${currentPathIndex + 1} (${path.length} 条线索): `,
        path.map(c => `${c.stage}:${c.display_level}:${c.from.type}→${c.to.type}`).join(' → ')
      );

      // 将路径中的每条线索标记为属于当前路径
      path.forEach(clue => {
        if (!cluePathMap.has(clue.id)) {
          cluePathMap.set(clue.id, {
            clue: clue,
            pathIndices: new Set([currentPathIndex])
          });
        } else {
          // 线索已存在，添加到新的路径索引
          cluePathMap.get(clue.id)!.pathIndices.add(currentPathIndex);
        }
      });
    });
  });

  // 5. 🔧 提取所有线索（已经保证每条线索都属于至少一条有效路径）
  const expandedClues = Array.from(cluePathMap.values()).map(cp => cp.clue);

  console.log(
    `精简模式: 从 ${finalClues.length} 个 final 节点反推出 ${totalPaths} 条路径, ` +
    `去重后显示 ${expandedClues.length} 条有效线索`
  );

  // 6. 🔧 输出路径共享统计
  const sharedClues = Array.from(cluePathMap.values())
    .filter(cp => cp.pathIndices.size > 1)
    .sort((a, b) => b.pathIndices.size - a.pathIndices.size);

  if (sharedClues.length > 0) {
    console.log(`📊 路径共享统计: ${sharedClues.length} 条线索被多条路径共享`);
    sharedClues.slice(0, 5).forEach(({ clue, pathIndices }) => {
      console.log(
        `  - ${clue.from.type}→${clue.to.type} (${clue.from.content?.substring(0, 20)}...): ` +
        `被 ${pathIndices.size} 条路径共享 (路径索引: ${Array.from(pathIndices).slice(0, 5).join(', ')})`
      );
    });
  }

  return expandedClues;
}

/**
 * 精简模式过滤器（增强版）
 *
 * 根据选中的阶段组合，智能反推并显示完整推理路径
 *
 * @param clues - 所有线索
 * @param mode - 显示模式 ('simplified' | 'full')
 * @param selectedStages - 选中的阶段列表
 * @returns 过滤后的线索列表
 */
export function filterCluesByDisplayMode(
  clues: Clue[],
  mode: 'simplified' | 'full',
  selectedStages: string[]
): Clue[] {
  if (mode === 'full') {
    // 全量模式：过滤掉 final 级别的线索，只显示 intermediate 和 debug
    // final 线索是为精简模式专门生成的，全量模式不需要
    return clues.filter(c => c.display_level !== 'final');
  }

  // 精简模式：根据选中阶段反推路径
  return expandFinalClues(clues, selectedStages);
}

/**
 * 统计不同 display_level 的线索数量
 */
export function countCluesByLevel(clues: Clue[]): {
  final: number;
  intermediate: number;
  debug: number;
  total: number;
} {
  const counts = {
    final: 0,
    intermediate: 0,
    debug: 0,
    total: clues.length,
  };

  clues.forEach(clue => {
    const level = clue.display_level || 'intermediate';
    if (level in counts) {
      counts[level as keyof typeof counts]++;
    }
  });

  return counts;
}

/**
 * 按阶段分组统计 final 线索
 */
export function countFinalCluesByStage(clues: Clue[]): Record<string, number> {
  const finalClues = clues.filter(c => c.display_level === 'final');
  const counts: Record<string, number> = {};

  finalClues.forEach(clue => {
    counts[clue.stage] = (counts[clue.stage] || 0) + 1;
  });

  return counts;
}

