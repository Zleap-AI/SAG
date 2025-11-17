'use client'

import React, { useState, useEffect, useRef } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { MentionsInput, Mention } from 'react-mentions'
import { motion, AnimatePresence } from 'framer-motion'
import dynamic from 'next/dynamic'
import {
  Search as SearchIcon,
  Loader2,
  Sparkles,
  Database,
  Zap,
  ChevronDown,
  Settings2,
  X,
  RotateCcw,
  Target,
  Search,
  GitBranch,
  BarChart3,
  ChevronRight,
  Save,
  Network,
  List,
  Clock,
  Trash2,
} from 'lucide-react'
import { apiClient } from '@/lib/api-client'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandList,
} from "@/components/ui/command"
import {
  Drawer,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerFooter,
  DrawerHeader,
  DrawerTitle,
  DrawerTrigger,
} from "@/components/ui/drawer"
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import { Label } from "@/components/ui/label"
import { Slider } from "@/components/ui/slider"
import { Switch } from "@/components/ui/switch"
import { Separator } from "@/components/ui/separator"
import { SEARCH_PARAM_GROUPS, getDefaultSearchParams } from '@/lib/search-config'
import { EventCard } from '@/components/events/EventCard'
import { SourceEvent, ArticleSection } from '@/types'
import { DocumentDetailDrawer } from '@/components/documents/DocumentDetailDrawer'
import { AISummary } from '@/components/search/AISummary'
import { AIAnalysis } from '@/components/search/AIAnalysis'
import type { Clue, QueryEntity } from '@/types/search-response'
import { toast } from 'sonner'
import {
  saveSearchParams,
  loadSearchParams,
} from '@/lib/search-params-storage'
import {
  type SearchHistoryItem,
  saveSearchHistory,
  loadSearchHistory,
  deleteSearchHistoryItem,
  clearSearchHistory,
  formatTimeLabel,
} from '@/lib/search-history-storage'

// 动态导入 CluesGraph 组件，禁用 SSR（relation-graph 需要浏览器环境）
const CluesGraph = dynamic(
  () => import('@/components/search/CluesGraph').then(mod => mod.CluesGraph),
  { ssr: false, loading: () => <div className="flex items-center justify-center h-96"><Loader2 className="w-8 h-8 animate-spin" /></div> }
)

export default function SearchPage() {
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<'fast' | 'normal'>('fast')
  const [results, setResults] = useState<SourceEvent[]>([])
  const [clues, setClues] = useState<Clue[]>([])
  const [hasSearched, setHasSearched] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)

  // 文档详情抽屉状态（统一事项和片段）
  const [isDetailDrawerOpen, setIsDetailDrawerOpen] = useState(false)
  const [selectedArticleId, setSelectedArticleId] = useState<string | null>(null)
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)
  const [detailDrawerView, setDetailDrawerView] = useState<'events' | 'sections'>('sections')
  const [highlightSectionIds, setHighlightSectionIds] = useState<string[]>([])

  // 事项展开状态
  const [expandedEventIds, setExpandedEventIds] = useState<Set<string>>(new Set())
  const [openEntities, setOpenEntities] = useState<Set<string>>(new Set())
  const [openReferences, setOpenReferences] = useState<Set<string>>(new Set())
  const [expandedReferenceSections, setExpandedReferenceSections] = useState<Set<string>>(new Set())

  // 从 query 中提取的信息源 IDs
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([])

  // 当前选中的 tab
  const [activeTab, setActiveTab] = useState<'events' | 'clues'>('events')

  // 图谱动态高度
  const [graphHeight, setGraphHeight] = useState(800)
  const graphContainerRef = useRef<HTMLDivElement>(null)

  // 搜索历史记录
  const [searchHistory, setSearchHistory] = useState<SearchHistoryItem[]>([])
  const [isHistoryPopoverOpen, setIsHistoryPopoverOpen] = useState(false)

  // 搜索参数 - 使用动态对象管理所有参数
  const [searchParams, setSearchParams] = useState<Record<string, number | boolean>>(() => {
    // 初始化时从 localStorage 加载
    if (typeof window !== 'undefined') {
      const loadedParams = loadSearchParams()
      if (loadedParams) {
        return loadedParams
      }
    }
    return getDefaultSearchParams()
  })

  // 图标映射
  const iconMap: Record<string, any> = {
    Target,
    Search,
    GitBranch,
    BarChart3,
  }

  // 输入框容器 ref，用于自动滚动
  const inputContainerRef = useRef<HTMLDivElement>(null)

  // 获取信息源列表
  const { data: sourcesData } = useQuery({
    queryKey: ['sources'],
    queryFn: () => apiClient.getSources(),
  })

  // 从URL参数获取source_config_id
  useEffect(() => {
    const urlParams = new URLSearchParams(window.location.search)
    const sourceIdParam = urlParams.get('source_config_id')
    if (sourceIdParam && sourcesData?.data) {
      const source = sourcesData.data.find((s: any) => s.id === sourceIdParam)
      if (source) {
        setQuery(`@[${source.name}](${source.id}) `)
        setSelectedSourceIds([source.id])
      }
    }
  }, [sourcesData])

  // 解析 query 中的 mentions，提取信息源 IDs
  useEffect(() => {
    const mentionRegex = /@\[([^\]]+)\]\(([^)]+)\)/g
    const matches = [...query.matchAll(mentionRegex)]
    const ids = matches.map(m => m[2])
    setSelectedSourceIds(ids)
  }, [query])

  // 自动滚动到输入框最右侧
  useEffect(() => {
    if (inputContainerRef.current) {
      setTimeout(() => {
        if (inputContainerRef.current) {
          inputContainerRef.current.scrollLeft = inputContainerRef.current.scrollWidth
        }
      }, 0)
    }
  }, [query])

  // 动态计算图谱高度
  useEffect(() => {
    const calculateGraphHeight = () => {
      if (typeof window === 'undefined') return

      // 获取视口高度
      const viewportHeight = window.innerHeight

      // 减去搜索框区域（约80px）
      // 减去 Tabs 切换区域（约60px）
      // 减去 Docker 栏预留空间（约80px）
      // 减去额外的安全边距（约40px）
      const reservedSpace = 80 + 60 + 80 + 40 + 150

      const calculatedHeight = viewportHeight - reservedSpace

      // 设置最小高度600px，最大高度1200px
      const finalHeight = Math.max(600, Math.min(1200, calculatedHeight))

      setGraphHeight(finalHeight)
    }

    // 初始计算
    calculateGraphHeight()

    // 监听窗口大小变化
    window.addEventListener('resize', calculateGraphHeight)

    return () => window.removeEventListener('resize', calculateGraphHeight)
  }, [])

  // 加载搜索历史记录
  useEffect(() => {
    if (typeof window !== 'undefined') {
      const history = loadSearchHistory()
      setSearchHistory(history)
    }
  }, [])

  const searchMutation = useMutation({
    mutationFn: (data: any) => apiClient.runSearch(data),
    onSuccess: (response) => {
      setResults(response.data?.events || [])
      setClues(response.data?.clues || [])
      setHasSearched(true)

      // 保存到搜索历史记录
      const plainText = query.replace(/@\[([^\]]+)\]\(([^)]+)\)/g, '').trim()
      if (plainText) {
        const sourceId = selectedSourceIds.length > 0 ? selectedSourceIds[0] : undefined
        const sourceName = sourceId && sourcesData?.data
          ? sourcesData.data.find((s: any) => s.id === sourceId)?.name
          : undefined

        saveSearchHistory({
          query,
          plainQuery: plainText,
          sourceId,
          sourceName,
          mode,
        })

        // 重新加载历史记录
        const updatedHistory = loadSearchHistory()
        setSearchHistory(updatedHistory)
      }
    },
  })

  const handleSearch = () => {
    // 提取纯文本（去除 mention 标记）
    const plainText = query.replace(/@\[([^\]]+)\]\(([^)]+)\)/g, '').trim()

    if (plainText) {
      // 关闭历史记录弹窗
      setIsHistoryPopoverOpen(false)

      // 合并所有参数
      searchMutation.mutate({
        source_config_ids: selectedSourceIds,
        query: plainText,
        mode,
        use_fast_mode: mode === 'fast',
        ...searchParams,  // 展开所有搜索参数
      })
    }
  }

  // 重置参数为默认值
  const handleResetParams = () => {
    setSearchParams(getDefaultSearchParams())
    toast.success('已重置为默认配置')
  }

  // 保存参数到 localStorage
  const handleSaveParams = () => {
    try {
      saveSearchParams(searchParams)
      toast.success('配置已保存')
      setDrawerOpen(false) // 保存后关闭抽屉
    } catch (error) {
      toast.error('保存失败，请重试')
    }
  }

  // 更新单个参数
  const handleParamChange = (key: string, value: number | boolean) => {
    setSearchParams(prev => ({
      ...prev,
      [key]: value,
    }))
  }

  // 选择历史记录
  const handleSelectHistory = (item: SearchHistoryItem) => {
    // 先更新 query 显示
    setQuery(item.query)
    setIsHistoryPopoverOpen(false)

    // 直接使用历史记录中的数据发起搜索，不依赖状态更新
    if (item.plainQuery) {
      searchMutation.mutate({
        source_config_ids: item.sourceId ? [item.sourceId] : [],
        query: item.plainQuery,
        mode: item.mode,
        use_fast_mode: item.mode === 'fast',
        ...searchParams,
      })
    }
  }

  // 删除单条历史记录
  const handleDeleteHistory = (id: string) => {
    deleteSearchHistoryItem(id)
    setSearchHistory(loadSearchHistory())
  }

  // 清空所有历史记录
  const handleClearHistory = () => {
    clearSearchHistory()
    setSearchHistory([])
    toast.success('已清空搜索历史')
  }

  // 处理片段跳转 - 使用统一的文档详情抽屉
  const handleNavigateToSections = (event: SourceEvent, sectionIds: string[]) => {
    setSelectedArticleId(event.article_id)  // 设置文档ID，加载完整文档
    setSelectedEventId(event.id)  // 设置事项ID，用于高亮和返回定位
    setDetailDrawerView('sections')  // 默认显示片段视图
    setHighlightSectionIds(sectionIds)  // 设置要高亮的片段ID
    setIsDetailDrawerOpen(true)  // 打开抽屉
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

  // 切换实体展开
  const toggleEntities = (id: string) => {
    setOpenEntities(prev => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  // 切换引用展开
  const toggleReferences = (id: string) => {
    setOpenReferences(prev => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  // 切换单个片段展开
  const toggleReferenceSection = (sectionId: string) => {
    setExpandedReferenceSections(prev => {
      const next = new Set(prev)
      if (next.has(sectionId)) {
        next.delete(sectionId)
      } else {
        next.add(sectionId)
      }
      return next
    })
  }

  const modeConfig = {
    fast: { label: '快速', icon: Zap, desc: '跳过属性抽取，快速召回' },
    normal: { label: '普通', icon: Sparkles, desc: 'LLM属性抽取，精确搜索' }
  }

  const CurrentIcon = modeConfig[mode].icon

  // react-mentions 样式（蓝色主题）
  const mentionStyle: any = {
    control: {
      backgroundColor: '#fff',
      fontSize: 15,
      fontWeight: 400,
      minHeight: 48,
      minWidth: '100%',
      width: 'auto',
      display: 'flex',
      alignItems: 'center',
    },
    '&singleLine': {
      display: 'inline-block',
      width: 'auto',
      minWidth: '100%',
      highlighter: {
        padding: '12px 40px 12px 14px',
        border: 0,
        lineHeight: '1.5',
        whiteSpace: 'nowrap',
        overflow: 'visible',
      },
      input: {
        padding: '12px 40px 12px 14px',
        border: 0,
        outline: 0,
        color: '#374151',
        caretColor: '#000',
        lineHeight: '1.5',
        whiteSpace: 'nowrap',
        overflow: 'visible',
      },
    },
    suggestions: {
      list: {
        position: 'fixed',
        backgroundColor: 'white',
        border: '1px solid #e5e7eb',
        borderRadius: '12px',
        boxShadow: '0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)',
        fontSize: 14,
        maxHeight: 320,
        overflow: 'auto',
        zIndex: 9999,
        marginTop: 8,
      },
      item: {
        padding: '12px 14px',
        borderBottom: '1px solid #f3f4f6',
        cursor: 'pointer',
        transition: 'background-color 0.15s ease',
        '&focused': {
          backgroundColor: '#eff6ff',
        },
      },
    },
  }

  const plainText = query.replace(/@\[([^\]]+)\]\(([^)]+)\)/g, '').trim()
  const hasContent = query.trim().length > 0
  const canSearch = plainText.length > 0
  
  // 从 clues 中提取 prepare 阶段数据（用于 AI 分析展示）
  const prepareData = React.useMemo(() => {
    const prepareClues = clues.filter(c => c.stage === 'prepare');
    
    // 查询重写
    const rewriteClue = prepareClues.find(c => 
      c.from.type === 'query' && 
      c.to.type === 'query' && 
      c.relation?.includes('重写')
    );
    
    // 提取的实体
    const extractedEntities: QueryEntity[] = prepareClues
      .filter(c => 
        c.from.type === 'query' && 
        c.to.type === 'entity' && 
        c.relation?.includes('属性提取')
      )
      .map(c => ({
        id: c.to.id,
        name: c.to.content,
        type: c.to.category,
        weight: c.confidence,
      }));
    
    return {
      originQuery: rewriteClue?.from.content || plainText,
      finalQuery: rewriteClue?.to.content || null,
      entities: extractedEntities,
    };
  }, [clues, plainText]);

  // 渲染搜索框组件（复用）
  const renderSearchBox = () => (
    <motion.div
      layout
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        duration: 0.6,
        delay: hasSearched ? 0 : 0.4,
        ease: [0.4, 0, 0.2, 1]
      }}
    >
      <div className="mx-auto max-w-3xl">
        <div className="flex items-center gap-0 border border-gray-200 rounded-lg bg-white shadow-sm hover:shadow-md focus-within:border-blue-400 focus-within:ring-2 focus-within:ring-blue-100 transition-all overflow-visible">
          {/* 左侧：搜索模式 */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                className="h-12 rounded-none rounded-l-lg border-r px-3 font-medium text-sm gap-1.5 shrink-0 hover:bg-gray-50"
              >
                <CurrentIcon className="h-4 w-4" />
                <span className="text-xs">{modeConfig[mode].label}</span>
                <ChevronDown className="h-3 w-3 opacity-50" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-56">
              <DropdownMenuRadioGroup value={mode} onValueChange={(v) => setMode(v as any)}>
                {Object.entries(modeConfig).map(([key, config]) => {
                  const Icon = config.icon
                  return (
                    <DropdownMenuRadioItem key={key} value={key} className="cursor-pointer py-2.5">
                      <Icon className="mr-2.5 h-4 w-4" />
                      <div className="flex flex-col">
                        <span className="text-sm font-medium">{config.label}</span>
                        <span className="text-xs text-muted-foreground">{config.desc}</span>
                      </div>
                    </DropdownMenuRadioItem>
                  )
                })}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>

          {/* 输入框 - react-mentions - 包装 Popover 显示历史记录 */}
          <Popover open={isHistoryPopoverOpen} onOpenChange={setIsHistoryPopoverOpen}>
            <PopoverTrigger asChild>
              <div
                ref={inputContainerRef}
                className="flex-1 overflow-x-auto overflow-y-visible scrollbar-hide relative"
                onFocusCapture={() => {
                  // 使用 capture phase 捕获子元素的 focus 事件
                  // 检查是否应该显示历史记录
                  const shouldShow = searchHistory.length > 0 && !query.trim().startsWith('@')
                  if (shouldShow) {
                    setTimeout(() => setIsHistoryPopoverOpen(true), 0)
                  }
                }}
                onClick={(e) => {
                  // 点击输入框时也尝试打开历史记录
                  const shouldShow = searchHistory.length > 0 && !query.trim().startsWith('@')
                  if (shouldShow) {
                    e.stopPropagation()
                    setTimeout(() => setIsHistoryPopoverOpen(true), 50)
                  }
                }}
              >
                <MentionsInput
                  value={query}
                  onChange={(e) => {
                    const newValue = e.target.value
                    setQuery(newValue)

                    // 检测是否正在输入 @
                    // 如果内容以 @ 开头或者正在输入未完成的 mention，关闭历史记录
                    const trimmedValue = newValue.trim()
                    if (trimmedValue.startsWith('@')) {
                      setIsHistoryPopoverOpen(false)
                    }
                  }}
                  placeholder="输入 @ 选择信息源，然后搜索..."
                  style={mentionStyle}
                  singleLine
                  onKeyDown={(e: any) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      handleSearch()
                    }
                  }}
                >
                  <Mention
                    trigger="@"
                    data={sourcesData?.data?.map((s: any) => ({
                      id: s.id,
                      display: s.name,
                    })) || []}
                    renderSuggestion={(suggestion: any, search, highlightedDisplay) => (
                      <div className="flex items-center gap-2">
                        <Database className="h-4 w-4 text-blue-600" />
                        <div>
                          <div className="font-medium">{highlightedDisplay}</div>
                          {sourcesData?.data?.find((s: any) => s.id === suggestion.id)?.description && (
                            <div className="text-xs text-muted-foreground">
                              {sourcesData.data.find((s: any) => s.id === suggestion.id).description}
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                    markup="@[__display__](__id__)"
                    displayTransform={(id, display) => `@${display}`}
                    appendSpaceOnAdd
                    style={{
                      backgroundColor: '#bfdbfe',
                      color: '#1e40af',
                      fontWeight: 600,
                      opacity: 1,
                      position: 'relative',
                      zIndex: 2,
                    }}
                  />
                </MentionsInput>

                {/* 清空按钮 - 悬浮样式 */}
                {hasContent && (
                  <button
                    onClick={() => setQuery('')}
                    className="absolute right-2 bottom-3 h-6 w-6 rounded-full bg-gray-200 hover:bg-gray-300 flex items-center justify-center transition-colors z-10"
                  >
                    <X className="h-3.5 w-3.5 text-gray-600" />
                  </button>
                )}
              </div>
            </PopoverTrigger>
            <PopoverContent
              className="w-[600px] p-0"
              align="start"
              onOpenAutoFocus={(e) => e.preventDefault()}
              onInteractOutside={(e) => {
                // 防止点击输入框时关闭
                const target = e.target as HTMLElement
                if (inputContainerRef.current?.contains(target)) {
                  e.preventDefault()
                }
              }}
            >
              <Command>
                <CommandList>
                  {searchHistory.length === 0 ? (
                    <CommandEmpty className="py-6 text-center text-sm text-muted-foreground">
                      暂无搜索历史
                    </CommandEmpty>
                  ) : (
                    <CommandGroup heading="搜索历史">
                      {searchHistory.map((item) => (
                        <div key={item.id} className="relative">
                          <div
                            className="flex items-start justify-between gap-3 px-3 py-2.5 cursor-pointer hover:bg-gray-50 rounded-sm"
                            onClick={() => {
                              handleSelectHistory(item)
                            }}
                          >
                            <div className="flex items-start gap-2.5 flex-1 min-w-0">
                              <Clock className="h-4 w-4 text-muted-foreground shrink-0 mt-0.5" />
                              <div className="flex-1 min-w-0 space-y-1">
                                {/* 查询文本 */}
                                <div className="text-sm font-medium text-gray-900 truncate">
                                  {item.plainQuery}
                                </div>
                                {/* 元信息行 */}
                                <div className="flex items-center gap-2 flex-wrap">
                                  {item.sourceName && (
                                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-medium bg-blue-50 text-blue-700 border border-blue-200">
                                      <Database className="h-3 w-3" />
                                      {item.sourceName}
                                    </span>
                                  )}
                                  <span className="text-xs text-muted-foreground">
                                    {formatTimeLabel(item.timestamp)}
                                  </span>
                                  <span className="text-xs text-muted-foreground">•</span>
                                  <span className="text-xs text-muted-foreground">
                                    {item.mode === 'fast' ? '快速' : '深度'}
                                  </span>
                                </div>
                              </div>
                            </div>
                            <button
                              className="h-6 w-6 p-0 shrink-0 rounded hover:bg-red-50 hover:text-red-600 transition-colors flex items-center justify-center"
                              onClick={(e) => {
                                e.preventDefault()
                                e.stopPropagation()
                                handleDeleteHistory(item.id)
                              }}
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </div>
                      ))}
                    </CommandGroup>
                  )}
                </CommandList>
                {searchHistory.length > 0 && (
                  <div className="border-t p-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="w-full text-xs text-muted-foreground hover:text-foreground"
                      onClick={(e) => {
                        e.preventDefault()
                        e.stopPropagation()
                        handleClearHistory()
                      }}
                    >
                      <Trash2 className="h-3 w-3 mr-1" />
                      清空历史记录
                    </Button>
                  </div>
                )}
              </Command>
            </PopoverContent>
          </Popover>

          {/* 右侧：配置 + 搜索 */}
          <div className="flex items-center shrink-0">
            {/* 高级参数 - Drawer */}
            <Drawer open={drawerOpen} onOpenChange={setDrawerOpen} direction="right">
              <DrawerTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-12 w-10 p-0 rounded-none border-l hover:bg-gray-50"
                >
                  <Settings2 className="h-4 w-4 text-muted-foreground" />
                </Button>
              </DrawerTrigger>
              <DrawerContent className="h-full w-[440px] ml-auto shadow-2xl">
                <div className="flex flex-col h-full">
                  {/* 头部 */}
                  <DrawerHeader className="border-b bg-gradient-to-b from-white to-gray-50/50">
                    <div className="flex items-center justify-between">
                      <div className="flex-1">
                        <DrawerTitle className="text-lg">搜索参数配置</DrawerTitle>
                        <DrawerDescription className="text-xs mt-1.5 text-muted-foreground">
                          调整各阶段搜索算法参数
                        </DrawerDescription>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={handleResetParams}
                        className="h-8 px-3 text-xs hover:bg-gray-100"
                      >
                        <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
                        重置
                      </Button>
                    </div>
                  </DrawerHeader>

                  {/* 内容区域 */}
                  <div className="flex-1 overflow-hidden p-6 bg-white" onPointerDown={(e) => e.stopPropagation()}>
                    <Tabs defaultValue="basic" className="h-full flex flex-col">
                      <TabsList className="grid w-full grid-cols-4 mb-6 bg-gray-100/80 p-1 rounded-lg">
                        {SEARCH_PARAM_GROUPS.map((group) => {
                          const Icon = iconMap[group.icon]
                          return (
                            <TabsTrigger
                              key={group.key}
                              value={group.key}
                              className="text-xs font-medium data-[state=active]:bg-white data-[state=active]:shadow-sm transition-all"
                            >
                              {Icon && <Icon className="h-3.5 w-3.5 mr-1.5" />}
                              {group.label.split(' ')[0]}
                            </TabsTrigger>
                          )
                        })}
                      </TabsList>

                      {SEARCH_PARAM_GROUPS.map((group) => (
                        <TabsContent
                          key={group.key}
                          value={group.key}
                          className="flex-1 overflow-y-auto space-y-6 pr-2 mt-0"
                        >
                          {group.params.map((param, index) => (
                            <motion.div
                              key={param.key}
                              initial={{ opacity: 0, y: 10 }}
                              animate={{ opacity: 1, y: 0 }}
                              transition={{ delay: index * 0.05 }}
                              className="space-y-3"
                            >
                              {param.type === 'boolean' ? (
                                // Boolean 参数 - Switch
                                <div className="flex items-center justify-between p-4 rounded-lg border border-gray-200 bg-gray-50/50 hover:bg-gray-100/50 transition-colors">
                                  <div className="space-y-1 flex-1 mr-4">
                                    <Label className="text-sm font-semibold text-gray-900 cursor-pointer">
                                      {param.label}
                                    </Label>
                                    <p className="text-xs text-muted-foreground leading-relaxed">
                                      {param.desc}
                                    </p>
                                  </div>
                                  <Switch
                                    checked={searchParams[param.key] as boolean}
                                    onCheckedChange={(checked) =>
                                      handleParamChange(param.key, checked)
                                    }
                                  />
                                </div>
                              ) : (
                                // Number 参数 - Slider
                                <div className="space-y-3 p-4 rounded-lg border border-gray-200 bg-white hover:border-blue-200 transition-colors">
                                  <div className="flex items-center justify-between">
                                    <Label className="text-sm font-semibold text-gray-900">
                                      {param.label}
                                    </Label>
                                    <span className="text-sm font-bold tabular-nums bg-blue-50 text-blue-700 px-2.5 py-1 rounded-md">
                                      {typeof searchParams[param.key] === 'number'
                                        ? param.step && param.step < 1
                                          ? (searchParams[param.key] as number).toFixed(2)
                                          : searchParams[param.key]
                                        : param.default}
                                    </span>
                                  </div>
                                  <Slider
                                    value={[searchParams[param.key] as number]}
                                    onValueChange={(value) =>
                                      handleParamChange(param.key, value[0])
                                    }
                                    min={param.min}
                                    max={param.max}
                                    step={param.step}
                                    className="w-full"
                                  />
                                  <p className="text-xs text-muted-foreground leading-relaxed">
                                    {param.desc}
                                  </p>
                                </div>
                              )}
                            </motion.div>
                          ))}
                        </TabsContent>
                      ))}
                    </Tabs>
                  </div>

                  {/* 底部 */}
                  <DrawerFooter className="border-t bg-gray-50/50 p-4">
                    <div className="flex items-center gap-2">
                      {/* 主要操作：保存 */}
                      <Button
                        onClick={handleSaveParams}
                        className="flex-1 h-10 bg-blue-600 hover:bg-blue-700 text-white font-medium shadow-sm"
                      >
                        <Save className="h-4 w-4 mr-2" />
                        保存配置
                      </Button>

                      {/* 关闭按钮 */}
                      <DrawerClose asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-10 w-10 hover:bg-gray-100"
                          aria-label="关闭"
                        >
                          <ChevronRight className="h-4 w-4" />
                        </Button>
                      </DrawerClose>
                    </div>
                  </DrawerFooter>
                </div>
              </DrawerContent>
            </Drawer>

            {/* 搜索按钮 */}
            <Button
              onClick={handleSearch}
              disabled={searchMutation.isPending || !canSearch}
              size="lg"
              className={cn(
                "h-12 px-4 md:px-5 rounded-lg font-medium transition-all",
                hasContent
                  ? "bg-blue-600 hover:bg-blue-700 text-white"
                  : "bg-gray-100 text-gray-400 cursor-not-allowed"
              )}
            >
              {searchMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <>
                  <SearchIcon className="h-4 w-4" />
                  <span className="ml-1.5 hidden md:inline text-sm"></span>
                </>
              )}
            </Button>
          </div>
        </div>

        {/* 提示 */}
        {!hasSearched && (
          <p className="text-center text-xs text-muted-foreground mt-3">
            输入 <kbd className="px-1.5 py-0.5 bg-muted rounded text-xs">@</kbd> 选择信息源，
            <kbd className="px-1.5 py-0.5 bg-muted rounded text-xs mx-1">Enter</kbd> 搜索
          </p>
        )}
      </div>
    </motion.div>
  )

  return (
    // 填充 layout 提供的空间
    <div className="h-full flex flex-col overflow-hidden">
      <AnimatePresence mode="wait">
        {/* 未搜索：居中显示 */}
        {!hasSearched ? (
          <motion.div
            key="centered"
            initial={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.3 }}
            style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          >
            <div className="w-full">
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.5 }}
              >
                <div className="text-center space-y-8">
                  {/* Icon */}
                  <motion.div
                    initial={{ opacity: 0, scale: 0.8 }}
                    animate={{ opacity: 1, scale: 1 }}
                    transition={{ duration: 0.5, delay: 0.2 }}
                  >
                    <div className="flex justify-center">
                      <div className="p-6 rounded-2xl bg-linear-to-br from-blue-100 to-purple-100 shadow-lg">
                        <SearchIcon className="w-16 h-16 text-blue-600" strokeWidth={1.5} />
                      </div>
                    </div>
                  </motion.div>

                  {/* Title */}
                  <motion.div
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.5, delay: 0.3 }}
                  >
                    <h1 className="text-4xl font-bold text-gray-800 mb-3">智能搜索</h1>
                    <p className="text-lg text-gray-500 max-w-xl mx-auto leading-relaxed">
                      支持快速和普通两种搜索模式
                    </p>
                  </motion.div>
                </div>
              </motion.div>

              {/* 搜索框 */}
              <div className="px-4 mt-16">
                {renderSearchBox()}
              </div>
            </div>
          </motion.div>
        ) : (
          /* 搜索后：搜索框上移 + 结果列表 */
          <motion.div
            key="search"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.4 }}
            style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
          >
            {/* 搜索框 - 从下方上移到顶部 */}
            <motion.div
              initial={{ y: 200, opacity: 0.5 }}
              animate={{ y: 0, opacity: 1 }}
              transition={{
                duration: 0.8,
                ease: [0.34, 1.05, 0.64, 1]  // 轻微弹性缓动
              }}
            >
              <div className="px-4 py-4">
                {renderSearchBox()}
              </div>
            </motion.div>

            {/* 搜索结果 - flex-1 填充剩余空间，内部滚动 */}
            <div className="flex-1 overflow-y-auto">
              <div className="px-4 pb-4">
                {/* 结果展示 - Tabs 切换 */}
                {results.length > 0 && (
                  <>
                    <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as 'events' | 'clues')} className="mt-0">
                      <div className="max-w-3xl mx-auto mb-5">
                        <div className="flex items-center justify-between">
                          <TabsList>
                            <TabsTrigger value="events" className="flex items-center gap-2">
                              <List className="w-4 h-4" />
                              列表
                              {results.length > 0 && (
                                <Badge variant="secondary" className="ml-1">
                                  {results.length}
                                </Badge>
                              )}
                            </TabsTrigger>
                            <TabsTrigger value="clues" className="flex items-center gap-2">
                              <Network className="w-4 h-4" />
                              图谱
                              {clues.length > 0 && (
                                <Badge variant="secondary" className="ml-1">
                                  {clues.length}
                                </Badge>
                              )}
                            </TabsTrigger>
                          </TabsList>

                          <div className="flex items-center gap-3">
                            <div className="flex items-center gap-2">
                              <p className="text-sm text-muted-foreground">
                                共 {results.length} 个结果
                              </p>
                              <Badge variant="outline" className="text-xs">{modeConfig[mode].label}</Badge>
                            </div>
                            <Button variant="ghost" size="sm" onClick={() => {
                              setHasSearched(false)
                              setResults([])
                              setClues([])
                              setActiveTab('events')
                            }}>
                              新搜索
                            </Button>
                          </div>
                        </div>
                      </div>

                      {/* Events 列表视图 */}
                      <TabsContent value="events" className="mt-0" forceMount>
                        <div className="max-w-3xl mx-auto" style={{ display: activeTab === 'events' ? 'block' : 'none' }}>
                          {/* AI 分析过程 - 展示 prepare 阶段数据（仅普通模式） */}
                          {mode === 'normal' && (prepareData.finalQuery || prepareData.entities.length > 0) && (
                            <motion.div
                              initial={{ opacity: 0, y: -10 }}
                              animate={{
                                opacity: activeTab === 'events' ? 1 : 0,
                                height: activeTab === 'events' ? 'auto' : 0,
                                marginBottom: activeTab === 'events' ? 16 : 0,
                              }}
                              transition={{ duration: 0.3, delay: 0.1 }}
                              style={{ overflow: 'hidden' }}
                            >
                              <AIAnalysis
                                originQuery={prepareData.originQuery}
                                finalQuery={prepareData.finalQuery}
                                queryEntities={prepareData.entities}
                              />
                            </motion.div>
                          )}
                          
                          {/* AI 智能总结 - 只在 Events tab 时显示 */}
                          {selectedSourceIds.length > 0 && (
                            <motion.div
                              initial={{ opacity: 0, y: -10 }}
                              animate={{
                                opacity: activeTab === 'events' ? 1 : 0,
                                height: activeTab === 'events' ? 'auto' : 0,
                                marginBottom: activeTab === 'events' ? 24 : 0,
                              }}
                              transition={{ duration: 0.3, delay: 0.2 }}
                              style={{ overflow: 'hidden' }}
                            >
                              <AISummary
                                sourceId={selectedSourceIds[0]}
                                query={query.replace(/@\[([^\]]+)\]\(([^)]+)\)/g, '').trim()}
                                eventIds={results.map(e => e.id)}
                                onReferenceClick={(eventId) => {
                                  // 点击引用，打开事项详情抽屉
                                  const event = results.find(e => e.id === eventId)
                                  if (event) {
                                    setSelectedArticleId(event.article_id)
                                    setSelectedEventId(eventId)
                                    setIsDetailDrawerOpen(true)
                                    setDetailDrawerView('events')
                                  }
                                }}
                              />
                            </motion.div>
                          )}

                          <motion.div
                            initial={{ opacity: 0, y: 10 }}
                            animate={{ opacity: 1, y: 0 }}
                            transition={{ duration: 0.3 }}
                          >
                            <div className="space-y-3">
                              {results.map((event, index) => (
                                <EventCard
                                  key={event.id}
                                  event={event}
                                  index={index}
                                  isExpanded={expandedEventIds.has(event.id)}
                                  isEntitiesOpen={openEntities.has(event.id)}
                                  isReferencesOpen={openReferences.has(event.id)}
                                  expandedReferenceSections={expandedReferenceSections}
                                  onToggleExpand={() => toggleEventExpand(event.id)}
                                  onToggleEntities={() => toggleEntities(event.id)}
                                  onToggleReferences={() => toggleReferences(event.id)}
                                  onToggleReferenceSection={toggleReferenceSection}
                                  onNavigateToSections={handleNavigateToSections}
                                />
                              ))}
                            </div>
                          </motion.div>
                        </div>
                      </TabsContent>

                      {/* Clues 图谱视图 */}
                      <TabsContent value="clues" className="mt-0">
                        <div className="max-w-9xl mx-auto">
                          <motion.div
                            initial={{ opacity: 0, y: 10 }}
                            animate={{ opacity: 1, y: 0 }}
                            transition={{ duration: 0.3 }}
                          >
                            <CluesGraph clues={clues} height={graphHeight} />
                          </motion.div>
                        </div>
                      </TabsContent>
                    </Tabs>
                  </>
                )}

                {/* 无结果 */}
                {results.length === 0 && !searchMutation.isPending && (
                  <motion.div
                    initial={{ opacity: 0, scale: 0.95 }}
                    animate={{ opacity: 1, scale: 1 }}
                    transition={{ duration: 0.4 }}
                  >
                    <div className="text-center mt-20">
                      <SearchIcon className="w-16 h-16 mx-auto text-muted-foreground/30 mb-4" />
                      <h3 className="text-lg font-medium mb-2">未找到相关结果</h3>
                      <p className="text-sm text-muted-foreground">尝试更换关键词或切换搜索模式</p>
                    </div>
                  </motion.div>
                )}

                {/* 错误 */}
                {searchMutation.isError && (
                  <motion.div
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.3 }}
                  >
                    <div className="mt-8">
                      <Card className="border-destructive/50 bg-destructive/5">
                        <CardContent className="py-4 text-center">
                          <p className="text-sm text-destructive">搜索失败，请检查配置后重试</p>
                        </CardContent>
                      </Card>
                    </div>
                  </motion.div>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* 文档详情抽屉（统一事项和片段，与文档页面一致） */}
      <DocumentDetailDrawer
        open={isDetailDrawerOpen}
        onClose={() => setIsDetailDrawerOpen(false)}
        articleId={selectedArticleId || undefined}
        defaultView={detailDrawerView}
        highlightSectionIds={highlightSectionIds}
        highlightEventId={selectedEventId || undefined}
      />

      {/* 添加全局样式 */}
      <style jsx global>{`
        .mentions__control {
          min-width: 100% !important;
          display: flex !important;
          align-items: center !important;
          width: auto !important;
        }

        .mentions__control > * {
          width: auto !important;
          min-width: 100% !important;
        }

        .mentions__input {
          border: none !important;
          outline: none !important;
          color: #374151 !important;
          caret-color: #000 !important;
          line-height: 1.5 !important;
          white-space: nowrap !important;
        }

        .mentions__highlighter {
          border: none !important;
          padding: 12px 40px 12px 14px !important;
          line-height: 1.5 !important;
          white-space: nowrap !important;
        }

        .mentions__mention {
          background-color: #bfdbfe !important;
          color: #1e40af !important;
          font-weight: 600 !important;
          opacity: 1 !important;
          position: relative !important;
          z-index: 2 !important;
        }

        .mentions__suggestions__list {
          position: fixed !important;
          background-color: white !important;
          border: 1px solid #e5e7eb !important;
          border-radius: 12px !important;
          box-shadow: 0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1) !important;
          z-index: 9999 !important;
          margin-top: 8px !important;
        }

        .mentions__suggestions__item {
          padding: 12px 14px !important;
          border-bottom: 1px solid #f3f4f6 !important;
          transition: background-color 0.15s ease !important;
        }

        .mentions__suggestions__item--focused {
          background-color: #eff6ff !important;
        }

        /* 隐藏滚动条但保持滚动功能 */
        .scrollbar-hide {
          -ms-overflow-style: none;
          scrollbar-width: none;
        }

        .scrollbar-hide::-webkit-scrollbar {
          display: none;
        }
      `}</style>
    </div>
  )
}
