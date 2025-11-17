"use client";

import React, { useMemo } from "react";
import { Badge } from "@/components/ui/badge";
import type { Clue } from "@/types/search-response";

interface GraphStatsProps {
  /** 线索数据 */
  clues: Clue[];
}

/**
 * 图谱统计面板
 *
 * 显示知识图谱的统计信息：
 * - 总节点数
 * - 总连线数
 */
export function GraphStats({ clues }: GraphStatsProps) {
  // 计算统计信息
  const stats = useMemo(() => {
    // 提取所有唯一节点
    const nodeIds = new Set<string>();

    clues.forEach((clue) => {
      nodeIds.add(clue.from.id);
      nodeIds.add(clue.to.id);
    });

    const totalNodes = nodeIds.size;
    const totalLines = clues.length;

    return {
      totalNodes,
      totalLines,
    };
  }, [clues]);

  return (
    <div className="flex items-center gap-3">
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">节点</span>
        <Badge variant="secondary" className="h-6 px-2 text-xs font-semibold">
          {stats.totalNodes}
        </Badge>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">连线</span>
        <Badge variant="secondary" className="h-6 px-2 text-xs font-semibold">
          {stats.totalLines}
        </Badge>
      </div>
    </div>
  );
}
