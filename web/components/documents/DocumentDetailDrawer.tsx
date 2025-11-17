'use client'

import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { 
  ListTodo, 
  BookOpen, 
  Calendar, 
  Tag, 
  ChevronDown, 
  Users, 
  ArrowLeft,
  ChevronRight
} from 'lucide-react'
import { apiClient } from '@/lib/api-client'
import { SourceEvent, ArticleSection } from '@/types'
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

interface DocumentDetailDrawerProps {
  open: boolean
  onClose: () => void
  articleId?: string
  defaultView?: 'events' | 'sections'
  highlightSectionIds?: string[]
  highlightEventId?: string  // 需要高亮的事项ID
}

type ViewType = 'events' | 'sections'

export function DocumentDetailDrawer({ 
  open, 
  onClose, 
  articleId,
  defaultView = 'events',
  highlightSectionIds = [],
  highlightEventId
}: DocumentDetailDrawerProps) {
  const [currentView, setCurrentView] = useState<ViewType>(defaultView)
  const [highlightedSections, setHighlightedSections] = useState<Set<string>>(new Set(highlightSectionIds))
  const [highlightedEvent, setHighlightedEvent] = useState<string | null>(highlightEventId || null)
  const [scrollToSection, setScrollToSection] = useState<string | null>(null)
  const [scrollToEvent, setScrollToEvent] = useState<string | null>(null)
  
  // 滚动容器 ref - 分别为两个视图
  const eventsScrollRef = useRef<HTMLDivElement>(null)
  const sectionsScrollRef = useRef<HTMLDivElement>(null)
  
  // 展开状态
  const [expandedEventIds, setExpandedEventIds] = useState<Set<string>>(new Set())
  const [expandedSectionIds, setExpandedSectionIds] = useState<Set<string>>(new Set())
  const [openEntities, setOpenEntities] = useState<Set<string>>(new Set())
  const [openReferences, setOpenReferences] = useState<Set<string>>(new Set())
  const [expandedReferenceSections, setExpandedReferenceSections] = useState<Set<string>>(new Set())

  // 获取事项数据
  const { data: eventsData, isLoading: isLoadingEvents } = useQuery({
    queryKey: ['documentEvents', articleId],
    queryFn: () => articleId ? apiClient.getDocumentEvents(articleId) : null,
    enabled: !!articleId && open,
  })

  // 获取片段数据
  const { data: sectionsData, isLoading: isLoadingSections } = useQuery({
    queryKey: ['documentSections', articleId],
    queryFn: () => articleId ? apiClient.getDocumentSections(articleId) : null,
    enabled: !!articleId && open,
  })

  const events = (eventsData?.data || []) as SourceEvent[]
  const sections = (sectionsData?.data || []) as ArticleSection[]

  // 当抽屉首次打开时设置默认视图
  const [isInitialized, setIsInitialized] = useState(false)
  
  useEffect(() => {
    if (open && !isInitialized) {
      setCurrentView(defaultView)
      
      // 设置高亮和滚动
      if (highlightSectionIds.length > 0) {
        setHighlightedSections(new Set(highlightSectionIds))
        // 如果默认显示片段视图且有高亮片段，自动滚动到第一个
        if (defaultView === 'sections') {
          setScrollToSection(highlightSectionIds[0])
        }
      }
      
      // 设置事项高亮
      if (highlightEventId) {
        setHighlightedEvent(highlightEventId)
        // 如果默认显示事项视图，自动滚动到高亮事项
        if (defaultView === 'events') {
          setScrollToEvent(highlightEventId)
        }
      }
      
      setIsInitialized(true)
    } else if (!open && isInitialized) {
      // 抽屉关闭时重置初始化状态，以便下次打开时能重新初始化
      setIsInitialized(false)
      setHighlightedEvent(null)
    }
  }, [open, defaultView, highlightSectionIds, highlightEventId, isInitialized])

  // 平滑滚动动画函数 - 使用 easeInOutCubic 缓动
  const smoothScrollTo = (container: HTMLElement, targetScroll: number, duration = 600) => {
    const startScroll = container.scrollTop
    const distance = targetScroll - startScroll
    const startTime = performance.now()
    
    // easeInOutCubic 缓动函数
    const easeInOutCubic = (t: number): number => {
      return t < 0.5 
        ? 4 * t * t * t 
        : 1 - Math.pow(-2 * t + 2, 3) / 2
    }
    
    const animateScroll = (currentTime: number) => {
      const elapsed = currentTime - startTime
      const progress = Math.min(elapsed / duration, 1)
      const eased = easeInOutCubic(progress)
      
      container.scrollTop = startScroll + distance * eased
      
      if (progress < 1) {
        requestAnimationFrame(animateScroll)
      }
    }
    
    requestAnimationFrame(animateScroll)
  }

  // 优化的滚动函数：等待数据加载完成和DOM渲染，带重试机制
  // scrollToTop: true 时优先将元素滚动到顶部，false 时使用智能定位
  const scrollToElement = (elementId: string, containerRef: React.RefObject<HTMLDivElement | null>, scrollToTop = true, maxRetries = 5, retryDelay = 200) => {
    let retries = 0
    
    const attemptScroll = () => {
      const element = document.getElementById(elementId)
      
      if (element && containerRef.current) {
        // 使用 requestAnimationFrame 确保 DOM 已完全渲染
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            const container = containerRef.current!
            const containerRect = container.getBoundingClientRect()
            const elementRect = element.getBoundingClientRect()
            
            // 计算元素相对于容器内部的位置
            const relativeTop = elementRect.top - containerRect.top
            const currentScroll = container.scrollTop
            
            let targetScroll: number
            
            if (scrollToTop) {
              // 优先模式：将元素滚动到容器顶部，留出 16px 间距
              targetScroll = currentScroll + relativeTop - 16
              
              // 确保不会滚动到负数位置
              targetScroll = Math.max(0, targetScroll)
            } else {
              // 智能模式：保持元素在视口中可见，留出 20px 间距
              targetScroll = currentScroll + relativeTop - 20
            }
            
            // 使用自定义平滑滚动，600ms 缓动动画
            smoothScrollTo(container, targetScroll, 600)
          })
        })
      } else if (retries < maxRetries) {
        // 元素未找到且未超过重试次数，继续重试
        retries++
        setTimeout(attemptScroll, retryDelay)
      }
    }
    
    // 等待视图淡入完成后再开始滚动（视图淡入动画 200ms + 50ms 缓冲）
    setTimeout(attemptScroll, 250)
  }

  // 自动滚动到高亮片段（优化版）
  useEffect(() => {
    if (scrollToSection && currentView === 'sections' && !isLoadingSections) {
      // 等待数据加载完成后才开始滚动，优先滚动到顶部
      scrollToElement(`section-${scrollToSection}`, sectionsScrollRef, true)
      setScrollToSection(null)
    }
  }, [scrollToSection, currentView, isLoadingSections])

  // 自动滚动到高亮事项（优化版）
  useEffect(() => {
    if (scrollToEvent && currentView === 'events' && !isLoadingEvents) {
      // 等待数据加载完成后才开始滚动，优先滚动到顶部
      scrollToElement(`event-${scrollToEvent}`, eventsScrollRef, true)
      setScrollToEvent(null)
    }
  }, [scrollToEvent, currentView, isLoadingEvents])

  // 切换视图（手动切换，清除高亮）
  const switchView = (view: ViewType) => {
    setCurrentView(view)
    setHighlightedSections(new Set())
    // 手动切换时也清除事项高亮
    setHighlightedEvent(null)
  }

  // 从事项跳转到片段
  const navigateToSections = (eventId: string, sectionIds: string[]) => {
    // 更新高亮的事项ID（用于返回时高亮和显示返回按钮）
    setHighlightedEvent(eventId)
    setHighlightedSections(new Set(sectionIds))
    setCurrentView('sections')
    if (sectionIds.length > 0) {
      setScrollToSection(sectionIds[0])
    }
  }

  // 返回事项视图
  const goBackToEvents = () => {
    setCurrentView('events')
    setHighlightedSections(new Set())
    // 返回时保持滚动位置，不需要滚动到高亮事项
    // 这样用户可以看到离开时的位置，体验更自然
    // 高亮效果仍然会显示（通过 highlightedEvent 状态）
  }

  // 切换事项展开
  const toggleEventExpand = (id: string) => {
    setExpandedEventIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  // 切换片段展开
  const toggleSectionExpand = (id: string) => {
    setExpandedSectionIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  // 只有数据真正不存在时才显示loading，避免切换时闪现loading
  const shouldShowLoading = currentView === 'events' 
    ? (isLoadingEvents && events.length === 0)
    : (isLoadingSections && sections.length === 0)

  return (
    <Sheet open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <SheetContent
        side="right"
        className="w-full sm:w-[650px] sm:max-w-[650px] p-0"
        overlayClassName="bg-black/20"
      >
        {/* 顶部导航栏 */}
        <div className="sticky top-0 z-10 bg-white border-b border-gray-200">
          <div className="px-6 py-4">
            {/* 标题和视图切换按钮在同一行 */}
            <div className="flex items-center justify-between mb-2">
              {/* 左侧：标题 */}
              <SheetTitle className="flex items-center gap-2">
                {currentView === 'events' ? (
                  <>
                    <ListTodo className="w-5 h-5 text-emerald-600" />
                    事项列表
                  </>
                ) : (
                  <>
                    <BookOpen className="w-5 h-5 text-blue-600" />
                    文章片段
                  </>
                )}
              </SheetTitle>

              {/* 右侧：视图切换按钮 */}
              <div className="flex gap-2">
                <button
                  onClick={() => switchView('events')}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                    currentView === 'events'
                      ? 'bg-emerald-50 text-emerald-700 shadow-sm'
                      : 'text-gray-600 hover:bg-gray-100'
                  }`}
                >
                  <ListTodo className="w-4 h-4" />
                  事项 <span className="text-xs">({events.length})</span>
                </button>
                <button
                  onClick={() => switchView('sections')}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                    currentView === 'sections'
                      ? 'bg-blue-50 text-blue-700 shadow-sm'
                      : 'text-gray-600 hover:bg-gray-100'
                  }`}
                >
                  <BookOpen className="w-4 h-4" />
                  片段 <span className="text-xs">({sections.length})</span>
                </button>
              </div>
            </div>

            {/* 描述 */}
            <SheetDescription className="text-sm">
              共 {currentView === 'events' ? events.length : sections.length} 个{currentView === 'events' ? '事项' : '片段'}
            </SheetDescription>

            {/* 返回按钮 - 放在描述下方 */}
            {highlightedEvent && currentView === 'sections' && (
              <button
                onClick={goBackToEvents}
                className="flex items-center gap-1.5 text-sm text-gray-600 hover:text-emerald-700 transition-colors mt-3 pt-3 border-t border-gray-100"
              >
                <ArrowLeft className="w-4 h-4" />
                返回事项
              </button>
            )}
          </div>
        </div>

        {/* 内容区域 */}
        <div 
          className="relative overflow-hidden" 
          style={{ 
            height: 'calc(100vh - 180px)',
            maxHeight: 'calc(100vh - 180px)'
          }}
        >
          {shouldShowLoading ? (
            <div className="flex items-center justify-center py-12">
              <div className="text-sm text-gray-500">加载中...</div>
            </div>
          ) : (
            <>
              {/* 事项视图 - 绝对定位，完全重叠 */}
              <motion.div
                ref={eventsScrollRef}
                className="absolute inset-0 w-full overflow-y-auto px-6 py-4"
                initial={false}
                animate={{
                  opacity: currentView === 'events' ? 1 : 0,
                  pointerEvents: currentView === 'events' ? 'auto' : 'none'
                }}
                transition={{ duration: 0.2 }}
              >
                <EventsView
                  events={events}
                  expandedIds={expandedEventIds}
                  openEntities={openEntities}
                  openReferences={openReferences}
                  expandedReferenceSections={expandedReferenceSections}
                  highlightedEventId={highlightedEvent}
                  onToggleExpand={toggleEventExpand}
                  onToggleEntities={setOpenEntities}
                  onToggleReferences={setOpenReferences}
                  onToggleReferenceSection={setExpandedReferenceSections}
                  onNavigateToSections={navigateToSections}
                />
              </motion.div>

              {/* 片段视图 - 绝对定位，完全重叠 */}
              <motion.div
                ref={sectionsScrollRef}
                className="absolute inset-0 w-full overflow-y-auto px-6 py-4"
                initial={false}
                animate={{
                  opacity: currentView === 'sections' ? 1 : 0,
                  pointerEvents: currentView === 'sections' ? 'auto' : 'none'
                }}
                transition={{ duration: 0.2 }}
              >
                <SectionsView
                  sections={sections}
                  expandedIds={expandedSectionIds}
                  highlightedIds={highlightedSections}
                  onToggleExpand={toggleSectionExpand}
                />
              </motion.div>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

// 事项视图组件
function EventsView({
  events,
  expandedIds,
  openEntities,
  openReferences,
  expandedReferenceSections,
  highlightedEventId,
  onToggleExpand,
  onToggleEntities,
  onToggleReferences,
  onToggleReferenceSection,
  onNavigateToSections,
}: {
  events: SourceEvent[]
  expandedIds: Set<string>
  openEntities: Set<string>
  openReferences: Set<string>
  expandedReferenceSections: Set<string>
  highlightedEventId: string | null
  onToggleExpand: (id: string) => void
  onToggleEntities: React.Dispatch<React.SetStateAction<Set<string>>>
  onToggleReferences: React.Dispatch<React.SetStateAction<Set<string>>>
  onToggleReferenceSection: React.Dispatch<React.SetStateAction<Set<string>>>
  onNavigateToSections: (eventId: string, sectionIds: string[]) => void
}) {
  if (events.length === 0) {
    return (
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="flex flex-col items-center justify-center py-12 text-center"
      >
        <ListTodo className="w-12 h-12 text-gray-300 mb-3" />
        <p className="text-sm text-gray-500">暂无事项</p>
      </motion.div>
    )
  }

  return (
    <div className="space-y-4">
      {events.map((event, index) => {
        const isExpanded = expandedIds.has(event.id)
        const isHighlighted = highlightedEventId === event.id
        const contentLength = event.content.length + (event.summary?.length || 0)
        const shouldShowToggle = contentLength > 500

        return (
          <motion.div
            key={event.id}
            id={`event-${event.id}`}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2 }}
          >
            <div className={`w-full text-left relative border-0 rounded-lg p-5 backdrop-blur-sm shadow-md hover:shadow-lg transition-all duration-300 ${
              isHighlighted 
                ? 'bg-emerald-50/80 ring-2 ring-emerald-400 animate-pulse-once' 
                : 'bg-white/80'
            }`}>
              {/* 排序号 */}
              <div className="absolute top-4 right-4">
                <Badge variant="outline" className={`text-xs ${
                  isHighlighted 
                    ? 'bg-emerald-100 text-emerald-800 border-emerald-300' 
                    : 'bg-emerald-50 text-emerald-700 border-emerald-200'
                }`}>
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
                onClick={() => shouldShowToggle && onToggleExpand(event.id)}
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
                  <div className={`absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t ${
                    isHighlighted 
                      ? 'from-emerald-50/80 via-emerald-50/80' 
                      : 'from-white via-white'
                  } to-transparent flex items-end justify-center pb-4`}>
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
                    open={openEntities.has(event.id)}
                    onOpenChange={(isOpen) => {
                      onToggleEntities(prev => {
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
                              {/* 实体列表 */}
                              <div className="space-y-1.5">
                                {entities
                                  .sort((a, b) => b.weight - a.weight)
                                  .map(entity => (
                                    <div key={entity.id} className="flex items-center gap-2 text-xs text-gray-700">
                                      <span className="w-1 h-1 rounded-full bg-emerald-400 shrink-0"></span>
                                      <span>[{entity.name}]</span><span>{entity.description}</span>
                                    </div>
                                  ))}
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
                    open={openReferences.has(event.id)}
                    onOpenChange={(isOpen) => {
                      onToggleReferences(prev => {
                        const next = new Set(prev)
                        if (isOpen) next.add(event.id)
                        else next.delete(event.id)
                        return next
                      })
                    }}
                  >
                    {/* 横向布局：左边折叠触发器 + 右边跳转按钮（固定位置） */}
                    <div className="flex items-center justify-between gap-2">
                      <CollapsibleTrigger className="flex items-center gap-2 text-xs text-gray-600 hover:text-gray-900 transition-colors">
                        <BookOpen className="w-3.5 h-3.5" />
                        <span>原文引用 ({event.references.length}个片段)</span>
                        <ChevronDown className={`w-3 h-3 transition-transform ${openReferences.has(event.id) ? 'rotate-180' : ''}`} />
                      </CollapsibleTrigger>
                      
                      {/* 查看原文按钮 - 固定在右侧 */}
                      <button
                        type="button"
                        onClick={(e) => {
                          e.preventDefault()
                          e.stopPropagation()
                          const sectionIds = event.references?.map(ref => ref.id) || []
                          onNavigateToSections(event.id, sectionIds)
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
                              if (isLong) {
                                onToggleReferenceSection(prev => {
                                  const next = new Set(prev)
                                  if (next.has(section.id)) {
                                    next.delete(section.id)
                                  } else {
                                    next.add(section.id)
                                  }
                                  return next
                                })
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
      })}
    </div>
  )
}

// 片段视图组件
function SectionsView({
  sections,
  expandedIds,
  highlightedIds,
  onToggleExpand,
}: {
  sections: ArticleSection[]
  expandedIds: Set<string>
  highlightedIds: Set<string>
  onToggleExpand: (id: string) => void
}) {
  if (sections.length === 0) {
    return (
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="flex flex-col items-center justify-center py-12 text-center"
      >
        <BookOpen className="w-12 h-12 text-blue-200 mb-3" />
        <p className="text-sm text-gray-500">暂无片段</p>
      </motion.div>
    )
  }

  return (
    <div className="space-y-4">
      {sections.map((section, index) => {
        const isExpanded = expandedIds.has(section.id)
        const isHighlighted = highlightedIds.has(section.id)
        const contentLength = section.content.length
        const shouldShowToggle = contentLength > 500

        return (
          <motion.div
            key={section.id}
            id={`section-${section.id}`}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2 }}
          >
            <button
              onClick={() => shouldShowToggle && onToggleExpand(section.id)}
              className={`w-full text-left relative border-0 rounded-lg p-5 backdrop-blur-sm shadow-md hover:shadow-lg transition-all duration-300 ${
                isHighlighted 
                  ? 'bg-blue-50/80 ring-2 ring-blue-400 animate-pulse-once' 
                  : 'bg-white/80'
              }`}
            >
              {/* 排序号 */}
              <div className="absolute top-4 right-4">
                <Badge 
                  variant="outline" 
                  className={`text-xs ${
                    isHighlighted 
                      ? 'bg-blue-100 text-blue-800 border-blue-300' 
                      : 'bg-blue-50 text-blue-700 border-blue-200'
                  }`}
                >
                  #{section.rank + 1}
                </Badge>
              </div>

              {/* 标题 */}
              <h3 className="text-base font-semibold text-gray-900 mb-3 pr-12">
                {section.heading || `片段 ${index + 1}`}
              </h3>

              {/* 内容 */}
              <div className="relative">
                <motion.p
                  initial={false}
                  animate={{ height: isExpanded ? 'auto' : 'fit-content' }}
                  transition={{ duration: 0.3, ease: 'easeInOut' }}
                  className={`text-sm text-gray-600 mb-4 whitespace-pre-wrap ${isExpanded ? '' : 'line-clamp-6'
                    }`}
                >
                  {section.content}
                </motion.p>

                {/* 折叠时的渐变遮罩 + 展开提示 */}
                {!isExpanded && shouldShowToggle && (
                  <div className={`absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t ${
                    isHighlighted ? 'from-blue-50/80 via-blue-50/80' : 'from-white via-white'
                  } to-transparent flex items-end justify-center pb-4`}>
                    <span className="text-xs text-blue-600 flex items-center gap-1 font-medium">
                      点击展开 <ChevronDown className="w-3 h-3" />
                    </span>
                  </div>
                )}
              </div>

              {/* 元数据 */}
              <div className="flex items-center justify-between text-xs text-gray-500 pt-3 border-t border-gray-100 mt-2">
                <span>创建时间: {formatDate(section.created_time)}</span>
                {section.extra_data?.type && (
                  <Badge variant="outline" className="text-xs bg-gray-50">
                    {section.extra_data.type}
                  </Badge>
                )}
              </div>
            </button>
          </motion.div>
        )
      })}
    </div>
  )
}

