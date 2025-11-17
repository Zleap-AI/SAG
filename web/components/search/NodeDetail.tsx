"use client";

import React from "react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import type { Node } from "@/types/search-response";

interface NodeDetailProps {
  /** 节点数据 */
  node: Node;
  /** 是否打开 */
  open: boolean;
  /** 状态变化回调 */
  onOpenChange: (open: boolean) => void;
}

/**
 * 节点详情面板
 *
 * 显示节点的完整信息：
 * - ID、类型、类别
 * - 内容和描述
 */
export function NodeDetail({ node, open, onOpenChange }: NodeDetailProps) {
  // 节点类型标签映射
  const typeLabels: Record<string, string> = {
    query: "查询",
    entity: "实体",
    event: "事项",
    section: "段落",
  };

  // 节点类型颜色映射
  const typeColors: Record<string, string> = {
    query: "bg-blue-500",
    entity: "bg-green-500",
    event: "bg-orange-500",
    section: "bg-purple-500",
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-[400px] sm:w-[540px]">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <span
              className={`w-3 h-3 rounded-full ${typeColors[node.type] || "bg-gray-500"}`}
            />
            {typeLabels[node.type] || node.type}详情
          </SheetTitle>
          <SheetDescription>节点的完整信息</SheetDescription>
        </SheetHeader>

        <div className="mt-6 space-y-6">
          {/* 基本信息 */}
          <div className="space-y-4">
            <div>
              <label className="text-sm font-medium text-muted-foreground">
                节点 ID
              </label>
              <p className="mt-1 font-mono text-sm break-all">
                {node.event_id || node.id}
              </p>
              {node.event_id && node.id !== node.event_id && (
                <p className="mt-1 font-mono text-xs text-muted-foreground break-all">
                  图谱 ID: {node.id}
                </p>
              )}
            </div>

            <div>
              <label className="text-sm font-medium text-muted-foreground">
                节点类型
              </label>
              <div className="mt-1">
                <Badge variant="outline">{typeLabels[node.type] || node.type}</Badge>
              </div>
            </div>

            <div>
              <label className="text-sm font-medium text-muted-foreground">
                类别
              </label>
              <div className="mt-1">
                <Badge variant="secondary">{node.category || "未分类"}</Badge>
              </div>
            </div>
          </div>

          <Separator />

          {/* 内容信息 */}
          <div className="space-y-4">
            <div>
              <label className="text-sm font-medium text-muted-foreground">
                内容
              </label>
              <p className="mt-1 text-sm">{node.content || "无内容"}</p>
            </div>

            {node.description && (
              <div>
                <label className="text-sm font-medium text-muted-foreground">
                  描述
                </label>
                <p className="mt-1 text-sm text-muted-foreground">
                  {node.description}
                </p>
              </div>
            )}
          </div>

          {/* 类型说明 */}
          <Separator />
          <div className="bg-muted/50 p-4 rounded-lg">
            <h4 className="text-sm font-medium mb-2">节点说明</h4>
            <p className="text-sm text-muted-foreground">
              {getNodeTypeDescription(node.type)}
            </p>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

/**
 * 获取节点类型说明
 */
function getNodeTypeDescription(type: string): string {
  const descriptions: Record<string, string> = {
    query: "查询节点代表用户的搜索请求，是整个知识图谱的起点。",
    entity: "实体节点代表从文档中提取的关键实体，如人物、主题、地点等。",
    event: "事项节点代表搜索返回的具体事项，包含标题、摘要和详细内容。",
    section: "段落节点代表文档中的特定章节，用于精确定位信息来源。",
  };

  return descriptions[type] || "未知节点类型";
}
