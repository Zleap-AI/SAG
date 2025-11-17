"use client";

import React, { useMemo } from "react";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import type { Clue } from "@/types/search-response";

interface StageFilterProps {
  /** 所有可用的阶段 */
  allStages: string[];
  /** 当前选中的阶段 */
  selectedStages: string[];
  /** 阶段变化回调 */
  onStagesChange: (stages: string[]) => void;
  /** 所有线索（用于统计） */
  clues: Clue[];
}

/**
 * 阶段过滤器组件
 *
 * 支持逐层展示搜索阶段：
 * - 模式1: 仅 Recall
 * - 模式2: Recall + Expand
 * - 模式3: Recall + Expand + Rerank
 *
 * 注意：Prepare 阶段始终包含（作为起点）
 */
export function StageFilter({
  allStages,
  selectedStages,
  onStagesChange,
  clues,
}: StageFilterProps) {
  // 统计每个阶段的线索数量
  const stageCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    allStages.forEach((stage) => {
      counts[stage] = clues.filter((clue) => clue.stage === stage).length;
    });
    return counts;
  }, [allStages, clues]);

  // 阶段标签映射
  const stageLabels: Record<string, string> = {
    prepare: "Prepare",
    recall: "Recall",
    expand: "Expand",
    rerank: "Rerank",
  };

  // 阶段颜色映射
  const stageColors: Record<string, string> = {
    prepare: "bg-purple-500",
    recall: "bg-blue-500",
    expand: "bg-green-500",
    rerank: "bg-orange-500",
  };

  // 🆕 逐层展示逻辑
  const toggleStage = (stage: string) => {
    // Prepare 阶段始终包含，不可取消
    if (stage === 'prepare') {
      return;
    }

    // 判断当前模式
    const hasRecall = selectedStages.includes('recall');
    const hasExpand = selectedStages.includes('expand');
    const hasRerank = selectedStages.includes('rerank');

    let newStages: string[] = ['prepare']; // 始终包含 prepare

    if (stage === 'recall') {
      // 点击 Recall
      if (hasRecall) {
        // 当前已有 Recall，点击取消 → 不允许（至少要有 Recall）
        newStages = ['prepare', 'recall'];
      } else {
        // 当前没有 Recall，点击添加 → 仅 Recall
        newStages = ['prepare', 'recall'];
      }
    } else if (stage === 'expand') {
      // 点击 Expand
      if (hasExpand) {
        // 当前已有 Expand，点击取消 → 回退到仅 Recall
        newStages = ['prepare', 'recall'];
      } else {
        // 当前没有 Expand，点击添加 → Recall + Expand
        newStages = ['prepare', 'recall', 'expand'];
      }
    } else if (stage === 'rerank') {
      // 点击 Rerank
      if (hasRerank) {
        // 当前已有 Rerank，点击取消 → 回退到 Recall + Expand
        newStages = ['prepare', 'recall', 'expand'];
      } else {
        // 当前没有 Rerank，点击添加 → Recall + Expand + Rerank
        newStages = ['prepare', 'recall', 'expand', 'rerank'];
      }
    }

    onStagesChange(newStages);
  };

  return (
    <div className="flex items-center gap-4">
      <span className="font-semibold text-gray-700 w-[40px] text-left text-xs">
        阶段
      </span>
      <div className="flex items-center gap-3">
        {allStages.map((stage) => {
          const isChecked = selectedStages.includes(stage);
          const count = stageCounts[stage] || 0;

          // 🆕 Prepare 始终选中且禁用（不可取消）
          const isPrepare = stage === 'prepare';
          // 🆕 判断是否可以点击：需要前置阶段都已选中
          const isClickable = stage === 'recall' ||
                             (stage === 'expand' && selectedStages.includes('recall')) ||
                             (stage === 'rerank' && selectedStages.includes('expand'));

          return (
            <div key={stage} className="flex items-center gap-1.5">
              <Checkbox
                id={`stage-${stage}`}
                checked={isChecked}
                onCheckedChange={() => toggleStage(stage)}
                disabled={count === 0 || isPrepare || !isClickable}
              />
              <Label
                htmlFor={`stage-${stage}`}
                className="flex items-center gap-1.5 cursor-pointer text-xs"
              >
                <span
                  className={`w-2 h-2 rounded-full ${stageColors[stage] || "bg-gray-500"}`}
                />
                <span>{stageLabels[stage] || stage}</span>
                <Badge variant="secondary" className="ml-0.5 h-5 px-1.5 text-xs">
                  {count}
                </Badge>
              </Label>
            </div>
          );
        })}
      </div>
    </div>
  );
}
