'use client'

import { motion } from 'framer-motion'
import {
  Calendar,
  Tag,
  ChevronDown,
  Users,
  BookOpen,
  ChevronRight
} from 'lucide-react'
import { SourceEvent } from '@/types'
import { Badge } from '@/components/ui/badge'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { formatDate } from '@/lib/utils'

interface EventCardProps {
  event: SourceEvent
  index?: number
  isExpanded: boolean
  isEntitiesOpen: boolean
  isReferencesOpen: boolean
  expandedReferenceSections?: Set<string>
  onToggleExpand: () => void
  onToggleEntities: () => void
  onToggleReferences: () => void
  onToggleReferenceSection?: (sectionId: string) => void
  onNavigateToSections: (event: SourceEvent, sectionIds: string[]) => void
  showSimilarity?: number  // 搜索相似度（可选）
}

export function EventCard({
  event,
  index = 0,
  isExpanded,
  isEntitiesOpen,
  isReferencesOpen,
  expandedReferenceSections = new Set(),
  onToggleExpand,
  onToggleEntities,
  onToggleReferences,
  onToggleReferenceSection,
  onNavigateToSections,
  showSimilarity,
}: EventCardProps) {
  const contentLength = event.content.length + (event.summary?.length || 0)
  const shouldShowToggle = contentLength > 500

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, delay: index * 0.05 }}
    >
      <div className="w-full text-left relative border-0 rounded-lg p-5 bg-white/80 backdrop-blur-sm shadow-md hover:shadow-lg transition-all duration-300">
        {/* 右上角标识 */}
        <div className="absolute top-4 right-4 flex items-center gap-2">
          {/* 搜索相似度（如果有） */}
          {showSimilarity !== undefined && (
            <Badge variant="outline" className="text-xs bg-blue-50 text-blue-700 border-blue-200">
              相似度 {(showSimilarity * 100).toFixed(1)}%
            </Badge>
          )}
          {/* 排序号 */}
          <Badge variant="outline" className="text-xs bg-emerald-50 text-emerald-700 border-emerald-200">
            #{event.rank + 1}
          </Badge>
        </div>

        {/* 标题 */}
        <h3 className="text-base font-semibold text-gray-900 mb-2 pr-12">
          {event.title}
        </h3>

        {/* 摘要 */}
        {event.summary && (
          <p className={`text-sm text-gray-600 mb-3 ${isExpanded ? '' : 'line-clamp-3'}`}>
            {event.summary}
          </p>
        )}

        {/* 内容 */}
        <div
          className="relative cursor-pointer"
          onClick={() => shouldShowToggle && onToggleExpand()}
        >
          <motion.p
            initial={false}
            animate={{ height: isExpanded ? 'auto' : 'fit-content' }}
            transition={{ duration: 0.3, ease: 'easeInOut' }}
            className={`text-sm text-gray-700 mb-4 whitespace-pre-wrap ${
              isExpanded ? '' : 'line-clamp-5'
            }`}
          >
            {event.content}
          </motion.p>

          {/* 折叠时的渐变遮罩 + 展开提示 */}
          {!isExpanded && shouldShowToggle && (
            <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-white via-white to-transparent flex items-end justify-center pb-4">
              <span className="text-xs text-emerald-600 flex items-center gap-1 font-medium">
                点击展开 <ChevronDown className="w-3 h-3" />
              </span>
            </div>
          )}
        </div>

        {/* 时间 */}
        {(event.start_time || event.end_time) && (
          <div className="flex items-center gap-2 text-xs text-gray-600 mb-3">
            <Calendar className="w-3.5 h-3.5" />
            {event.start_time && <span>{formatDate(event.start_time)}</span>}
            {event.start_time && event.end_time && <span>-</span>}
            {event.end_time && <span>{formatDate(event.end_time)}</span>}
          </div>
        )}

        {/* 标签 */}
        {event.extra_data?.tags && event.extra_data.tags.length > 0 && (
          <div className="flex items-center gap-2 flex-wrap mb-3">
            <Tag className="w-3.5 h-3.5 text-gray-500" />
            {event.extra_data.tags.map((tag, i) => (
              <Badge key={i} variant="outline" className="text-xs bg-gray-50 text-gray-600 border-gray-200">
                {tag}
              </Badge>
            ))}
          </div>
        )}

        {/* 关联实体 - 按类型分组 */}
        {event.entities && event.entities.length > 0 && (
          <div className="mb-3">
            <Collapsible
              open={isEntitiesOpen}
              onOpenChange={onToggleEntities}
            >
              <CollapsibleTrigger className="flex items-center gap-2 text-xs text-gray-600 hover:text-gray-900 transition-colors">
                <Users className="w-3.5 h-3.5" />
                <span>关联实体 ({event.entities.length}个)</span>
                <ChevronDown className={`w-3 h-3 transition-transform ${isEntitiesOpen ? 'rotate-180' : ''}`} />
              </CollapsibleTrigger>
              <CollapsibleContent className="mt-2">
                {(() => {
                  // 按类型分组
                  const grouped = event.entities.reduce((acc, entity) => {
                    if (!acc[entity.type]) {
                      acc[entity.type] = []
                    }
                    acc[entity.type].push(entity)
                    return acc
                  }, {} as Record<string, typeof event.entities>)

                  // 计算每个类型的平均权重并排序（先权重，再个数）
                  const sortedTypes = Object.entries(grouped)
                    .map(([type, entities]) => {
                      const avgWeight = entities.reduce((sum, e) => sum + e.weight, 0) / entities.length
                      return {
                        type,
                        entities,
                        avgWeight
                      }
                    })
                    .sort((a, b) => {
                      // 先按平均权重降序
                      if (b.avgWeight !== a.avgWeight) {
                        return b.avgWeight - a.avgWeight
                      }
                      // 权重相同时按个数降序
                      return b.entities.length - a.entities.length
                    })

                  return sortedTypes.map(({ type, entities, avgWeight }, index) => (
                    <div key={type}>
                      {/* 分割线（第一个分组不显示） */}
                      {index > 0 && (
                        <div className="my-2 border-t border-gray-100"></div>
                      )}
                      <div className="ml-5 p-3 bg-emerald-50/30 rounded-lg border border-emerald-100">
                        {/* 类型标题 */}
                        <div className="flex items-center gap-2 mb-2">
                          <Badge variant="outline" className="text-xs font-semibold bg-white">
                            {type}
                          </Badge>
                          <span className="text-xs text-gray-500">
                            {avgWeight.toFixed(1)} · {entities.length}个
                          </span>
                        </div>
                        {/* 实体卡片列表 - 精致展示 */}
                        <div className="space-y-2">
                          {entities
                            .sort((a, b) => b.weight - a.weight)
                            .map(entity => {
                              // 调试：检查 description 字段
                              console.log('Entity:', {
                                name: entity.name,
                                description: entity.description,
                                hasDescription: !!entity.description
                              });
                              
                              return (
                                <div 
                                  key={entity.id} 
                                  className="flex items-start gap-3 p-2.5 bg-white rounded-md border border-gray-200/60 hover:border-emerald-300 hover:shadow-sm transition-all duration-200"
                                >
                                  {/* 左侧：实体名称 + 权重 */}
                                  <div className="flex-1 min-w-0">
                                    <div className="flex items-center gap-2 mb-0.5">
                                      <span className="font-medium text-sm text-gray-900">
                                        {entity.name}
                                      </span>
                                      {entity.weight > 1 && (
                                        <Badge variant="secondary" className="text-xs px-1.5 py-0">
                                          ×{entity.weight.toFixed(1)}
                                        </Badge>
                                      )}
                                    </div>
                                    {/* 实体描述 - 灰色小字 */}
                                    {entity.description && entity.description.trim() && (
                                      <div className="text-xs text-gray-600 leading-relaxed pl-2 border-l-2 border-emerald-200 mt-1">
                                        {entity.description}
                                      </div>
                                    )}
                                  </div>
                                </div>
                              );
                            })}
                        </div>
                      </div>
                    </div>
                  ))
                })()}
              </CollapsibleContent>
            </Collapsible>
          </div>
        )}

        {/* 原文引用 - 可折叠预览 + 快捷跳转 */}
        {event.references && event.references.length > 0 && (
          <div className="mb-3">
            <Collapsible
              open={isReferencesOpen}
              onOpenChange={onToggleReferences}
            >
              {/* 横向布局：左边折叠触发器 + 右边跳转按钮（固定位置） */}
              <div className="flex items-center justify-between gap-2">
                <CollapsibleTrigger className="flex items-center gap-2 text-xs text-gray-600 hover:text-gray-900 transition-colors">
                  <BookOpen className="w-3.5 h-3.5" />
                  <span>原文引用 ({event.references.length}个片段)</span>
                  <ChevronDown className={`w-3 h-3 transition-transform ${isReferencesOpen ? 'rotate-180' : ''}`} />
                </CollapsibleTrigger>
                
                {/* 查看原文按钮 - 固定在右侧 */}
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault()
                    e.stopPropagation()
                    const sectionIds = event.references?.map(ref => ref.id) || []
                    onNavigateToSections(event, sectionIds)
                  }}
                  className="flex items-center gap-1 text-xs font-medium text-emerald-600 hover:text-emerald-700 transition-colors shrink-0"
                >
                  <span>查看原文</span>
                  <ChevronRight className="w-3 h-3" />
                </button>
              </div>
              
              <CollapsibleContent className="mt-2 space-y-2">
                {/* 片段预览列表 */}
                {event.references?.map((section) => {
                  const isExpanded = expandedReferenceSections.has(section.id)
                  const isLong = section.content.length > 200
                  
                  return (
                    <div 
                      key={section.id} 
                      className="ml-5 p-3 bg-emerald-50/30 rounded-lg border border-emerald-100 cursor-pointer"
                      onClick={() => {
                        if (isLong && onToggleReferenceSection) {
                          onToggleReferenceSection(section.id)
                        }
                      }}
                    >
                      {/* 片段标题 */}
                      <div className="flex items-center gap-2 mb-2">
                        <Badge variant="outline" className="text-xs bg-white shrink-0">
                          #{section.rank + 1}
                        </Badge>
                        <span className="text-xs font-semibold text-gray-800">
                          {section.heading || `片段`}
                        </span>
                      </div>
                      
                      {/* 片段内容 */}
                      <div className="relative">
                        <p className={`text-xs text-gray-700 leading-relaxed whitespace-pre-wrap ${isExpanded ? '' : 'line-clamp-3'}`}>
                          {section.content}
                        </p>
                        
                        {/* 折叠时的渐变遮罩 + 展开提示 */}
                        {!isExpanded && isLong && (
                          <div className="absolute bottom-0 left-0 right-0 h-8 bg-gradient-to-t from-emerald-50/30 via-emerald-50/30 to-transparent flex items-end justify-center pb-2">
                            <span className="text-xs text-emerald-600 flex items-center gap-1 font-medium">
                              点击展开 <ChevronDown className="w-2.5 h-2.5" />
                            </span>
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </CollapsibleContent>
            </Collapsible>
          </div>
        )}
      </div>
    </motion.div>
  )
}
