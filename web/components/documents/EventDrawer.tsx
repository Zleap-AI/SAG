'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { ListTodo, Calendar, Tag, ChevronDown, Users, BookOpen } from 'lucide-react'
import { apiClient } from '@/lib/api-client'
import { SourceEvent } from '@/types'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Badge } from '@/components/ui/badge'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { formatDate } from '@/lib/utils'

interface EventDrawerProps {
  open: boolean
  onClose: () => void
  articleId?: string
}

export function EventDrawer({ open, onClose, articleId }: EventDrawerProps) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const [openEntities, setOpenEntities] = useState<Set<string>>(new Set())
  const [openReferences, setOpenReferences] = useState<Set<string>>(new Set())

  const { data: eventsData, isLoading } = useQuery({
    queryKey: ['documentEvents', articleId],
    queryFn: () => articleId ? apiClient.getDocumentEvents(articleId) : null,
    enabled: !!articleId && open,
  })

  const events = (eventsData?.data || []) as SourceEvent[]

  const toggleExpand = (id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  return (
    <Sheet open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <SheetContent
        side="right"
        className="w-full sm:w-[650px] sm:max-w-[650px]"
        overlayClassName="bg-black/20"
      >
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <ListTodo className="w-5 h-5 text-emerald-600" />
            事项列表
          </SheetTitle>
          <SheetDescription>
            共 {events.length} 个事项
          </SheetDescription>
        </SheetHeader>

        <div className="mt-6 space-y-4 overflow-y-auto" style={{ maxHeight: 'calc(100vh - 140px)' }}>
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <div className="text-sm text-gray-500">加载中...</div>
            </div>
          ) : events.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <ListTodo className="w-12 h-12 text-gray-300 mb-3" />
              <p className="text-sm text-gray-500">暂无事项</p>
            </div>
          ) : (
            events.map((event, index) => {
              const isExpanded = expandedIds.has(event.id)
              const contentLength = event.content.length + (event.summary?.length || 0)
              const shouldShowToggle = contentLength > 500

              return (
                <motion.div
                  key={event.id}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.2, delay: index * 0.05 }}
                >
                  <div className="w-full text-left relative border-0 rounded-lg p-5 bg-white/80 backdrop-blur-sm shadow-md hover:shadow-lg transition-all duration-300">
                    {/* 排序号 */}
                    <div className="absolute top-4 right-4">
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
                      onClick={() => shouldShowToggle && toggleExpand(event.id)}
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

                    {/* 关联实体 */}
                    {event.entities && event.entities.length > 0 && (
                      <div className="mb-3">
                        <Collapsible
                          open={openEntities.has(event.id)}
                          onOpenChange={(isOpen) => {
                            setOpenEntities(prev => {
                              const next = new Set(prev)
                              if (isOpen) next.add(event.id)
                              else next.delete(event.id)
                              return next
                            })
                          }}
                        >
                          <CollapsibleTrigger className="flex items-center gap-2 text-xs text-gray-600 hover:text-gray-900 transition-colors">
                            <Users className="w-3.5 h-3.5" />
                            <span>关联实体 ({event.entities.length}个)</span>
                            <ChevronDown className={`w-3 h-3 transition-transform ${openEntities.has(event.id) ? 'rotate-180' : ''}`} />
                          </CollapsibleTrigger>
                          <CollapsibleContent className="mt-2 ml-5 space-y-1.5">
                            {event.entities.map(entity => (
                              <div key={entity.id} className="flex items-center gap-2 text-xs">
                                <Badge variant="secondary" className="text-xs px-1.5 py-0">
                                  {entity.type}
                                </Badge>
                                <span className="text-gray-700">{entity.name}</span>
                                {entity.weight > 1 && (
                                  <span className="text-gray-400 text-xs">({entity.weight.toFixed(1)})</span>
                                )}
                              </div>
                            ))}
                          </CollapsibleContent>
                        </Collapsible>
                      </div>
                    )}

                    {/* 原文引用 */}
                    {event.references && event.references.length > 0 && (
                      <div className="mb-3">
                        <Collapsible
                          open={openReferences.has(event.id)}
                          onOpenChange={(isOpen) => {
                            setOpenReferences(prev => {
                              const next = new Set(prev)
                              if (isOpen) next.add(event.id)
                              else next.delete(event.id)
                              return next
                            })
                          }}
                        >
                          <CollapsibleTrigger className="flex items-center gap-2 text-xs text-gray-600 hover:text-gray-900 transition-colors">
                            <BookOpen className="w-3.5 h-3.5" />
                            <span>原文引用 ({event.references.length}个片段)</span>
                            <ChevronDown className={`w-3 h-3 transition-transform ${openReferences.has(event.id) ? 'rotate-180' : ''}`} />
                          </CollapsibleTrigger>
                          <CollapsibleContent className="mt-2 space-y-2">
                            {event.references.map((section) => (
                              <div key={section.id} className="ml-5 p-3 bg-blue-50/50 rounded-lg border border-blue-200">
                                <div className="flex items-center gap-2 mb-2">
                                  <Badge variant="outline" className="text-xs bg-white">
                                    #{section.rank + 1}
                                  </Badge>
                                  <span className="text-xs font-semibold text-gray-800">
                                    {section.heading}
                                  </span>
                                </div>
                                <p className="text-xs text-gray-700 leading-relaxed line-clamp-3">
                                  {section.content}
                                </p>
                              </div>
                            ))}
                          </CollapsibleContent>
                        </Collapsible>
                      </div>
                    )}

                    {/* 元数据 */}
                    <div className="flex items-center justify-between text-xs text-gray-500 pt-3 border-t border-gray-100 mt-2">
                      <span>创建时间: {formatDate(event.created_time)}</span>
                      <div className="flex items-center gap-2">
                        {event.extra_data?.category && (
                          <Badge variant="outline" className="text-xs bg-blue-50 text-blue-700 border-blue-200">
                            {event.extra_data.category}
                          </Badge>
                        )}
                        {event.extra_data?.priority && (
                          <Badge variant="outline" className="text-xs bg-amber-50 text-amber-700 border-amber-200">
                            {event.extra_data.priority}
                          </Badge>
                        )}
                      </div>
                    </div>
                  </div>
                </motion.div>
              )
            })
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
