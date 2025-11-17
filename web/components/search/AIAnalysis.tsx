"use client";

import React, { useState } from "react";
import { motion } from "framer-motion";
import { Brain, ChevronDown, ChevronRight } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { QueryEntity } from "@/types/search-response";

interface AIAnalysisProps {
  /** 原始查询 */
  originQuery: string;
  /** LLM 重写后的查询 */
  finalQuery: string | null;
  /** LLM 从查询中提取的实体 */
  queryEntities: QueryEntity[];
  /** 自定义样式 */
  className?: string;
  /** 默认展开状态 */
  defaultOpen?: boolean;
}

/**
 * AI 查询分析组件
 *
 * 显示 LLM 对查询的分析结果：
 * - 查询重写（如果发生）
 * - 从查询中提取的实体
 *
 * 仅在 normal 模式下且有数据时显示
 */
export function AIAnalysis({
  originQuery,
  finalQuery,
  queryEntities,
  className,
  defaultOpen = true,
}: AIAnalysisProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  // 判断是否有查询重写
  const isQueryRewritten = finalQuery && finalQuery !== originQuery;

  // 如果没有任何数据，不渲染
  if (!originQuery && queryEntities.length === 0) {
    return null;
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className={cn("mb-4", className)}
    >
      <Card className="border-blue-200 bg-blue-50/30">
        <div className="px-5 py-4">
          {/* Header */}
          <div
            className="flex items-center justify-between cursor-pointer mb-3"
            onClick={() => setIsOpen(!isOpen)}
          >
            <div className="flex items-center gap-2">
              <Brain className="h-4 w-4 text-blue-600" />
              <span className="text-sm font-semibold text-gray-900">
                AI 分析过程
              </span>
              <Badge variant="outline" className="text-xs bg-white">
                {isQueryRewritten ? "已重写" : "无重写"}
              </Badge>
              {queryEntities.length > 0 && (
                <span className="text-xs text-gray-500">
                  {queryEntities.length}个实体
                </span>
              )}
            </div>
            <div className="text-gray-400">
              {isOpen ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
            </div>
          </div>

          {/* Content */}
          {isOpen && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2 }}
              className="space-y-3"
            >
              {/* Query Rewrite Section */}
              {isQueryRewritten && (
                <div className="p-3 bg-yellow-50/70 rounded-lg border border-yellow-200/80">
                  <div className="flex items-center gap-2 mb-2">
                    <Badge
                      variant="outline"
                      className="text-xs bg-white border-yellow-300"
                    >
                      查询重写
                    </Badge>
                  </div>
                  <div className="space-y-1.5 text-sm">
                    <div className="flex items-start gap-2">
                      <span className="text-gray-500 shrink-0 min-w-[48px]">
                        原始:
                      </span>
                      <span className="text-gray-700">{originQuery}</span>
                    </div>
                    <div className="flex items-start gap-2">
                      <span className="text-gray-500 shrink-0 min-w-[48px]">
                        改写:
                      </span>
                      <span className="text-gray-900 font-medium">
                        {finalQuery}
                      </span>
                    </div>
                  </div>
                </div>
              )}

              {/* Query Entities Section */}
              {queryEntities.length > 0 && (
                <div className="p-3 bg-blue-50/70 rounded-lg border border-blue-200/80">
                  <div className="flex items-center gap-2 mb-2.5">
                    <Badge
                      variant="outline"
                      className="text-xs bg-white border-blue-300"
                    >
                      识别实体
                    </Badge>
                    <span className="text-xs text-gray-500">
                      从查询中提取的实体
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {queryEntities.map((entity) => (
                      <Badge
                        key={entity.id}
                        variant="outline"
                        className="text-xs bg-white hover:bg-blue-100 cursor-default border-blue-200"
                      >
                        <span className="font-medium text-gray-900">
                          {entity.name}
                        </span>
                        <span className="ml-1.5 text-gray-500">
                          ({entity.type})
                        </span>
                        {entity.weight > 1 && (
                          <span className="ml-1.5 text-blue-600 font-semibold">
                            ×{entity.weight}
                          </span>
                        )}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}

              {/* Empty State */}
              {!isQueryRewritten && queryEntities.length === 0 && (
                <div className="text-center text-sm text-gray-500 py-2">
                  无查询分析数据
                </div>
              )}
            </motion.div>
          )}
        </div>
      </Card>
    </motion.div>
  );
}
