'use client'

import React, { useState, useEffect, useRef, Suspense } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'next/navigation'
import { MentionsInput, Mention } from 'react-mentions'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import {
  MessageSquare,
  Database,
  Zap,
  FileText,
  ChevronDown,
  Send,
  X,
  User,
  Bot,
  Settings2,
  Brain,
  Loader2,
} from 'lucide-react'
import { apiClient } from '@/lib/api-client'
import { Button } from '@/components/ui/button'
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
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { Label } from "@/components/ui/label"
import { Slider } from "@/components/ui/slider"
import { cn } from '@/lib/utils'
import { DocumentDetailDrawer } from '@/components/documents/DocumentDetailDrawer'

interface SearchStatus {
  status: 'searching' | 'done'
  message?: string
  sourcesCount?: number
  eventsCount?: number
  confidence?: number
}

interface ThinkingStep {
  stage: string
  label: string
  content: string
  status: 'done' | 'processing'
}

interface Reference {
  order: number
  id: string
  title: string
  summary: string
  article_id?: string  // 🆕 文章ID，用于打开详情抽屉
}

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  
  // 搜索状态（简洁概览）
  searchStatus?: SearchStatus
  
  // 执行步骤（详细流程）
  thinkingSteps?: ThinkingStep[]
  
  // 实时推理（思考内容）
  reasoning?: string
  
  // 引用来源
  references?: Reference[]
  
  // 用户消息的信息源
  sources?: string[]
  
  // 统计信息
  stats?: {
    mode?: string
    events_found?: number
    confidence?: number
    sources?: number
    search_rounds?: number
    thinking_time?: number  // 思考耗时（秒）
  }
  
  timestamp: Date
  isStreaming?: boolean
}

function ChatPageContent() {
  const searchParams = useSearchParams()
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<'quick' | 'deep'>('quick')
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([])
  const [messages, setMessages] = useState<Message[]>([])
  const [showSettings, setShowSettings] = useState(false)

  // AI参数
  const [topK, setTopK] = useState<number[]>([10])
  const [resultStyle, setResultStyle] = useState<'concise' | 'detailed'>('concise')
  
  // 🆕 思考框展开状态（每条消息独立控制）
  const [thinkingOpenStates, setThinkingOpenStates] = useState<Record<string, boolean>>({})

  const inputContainerRef = useRef<HTMLDivElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const hasStarted = messages.length > 0

  // 🆕 文档详情抽屉状态
  const [isDetailDrawerOpen, setIsDetailDrawerOpen] = useState(false)
  const [selectedArticleId, setSelectedArticleId] = useState<string | null>(null)
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)
  const [detailDrawerView] = useState<'events' | 'sections'>('events')

  // 获取信息源列表
  const { data: sourcesData } = useQuery({
    queryKey: ['sources'],
    queryFn: () => apiClient.getSources(),
  })

  // 🆕 从 URL 参数读取 source_ids 并自动选中
  useEffect(() => {
    const sourceIdsParam = searchParams.get('source_ids')
    // 只在初次加载且 query 为空时自动添加
    if (sourceIdsParam && sourcesData?.data && !query) {
      const source = sourcesData.data.find((s: { id: string }) => s.id === sourceIdsParam)
      if (source) {
        // 自动在输入框中添加 @ 信息源
        const mentionText = `@[${source.name}](${source.id}) `
        setQuery(mentionText)
        setSelectedSourceIds([sourceIdsParam])
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, sourcesData])

  // 解析 query 中的 mentions
  useEffect(() => {
    // 只有当 query 中有内容时才解析 mentions
    if (!query) return
    
    const mentionRegex = /@\[([^\]]+)\]\(([^)]+)\)/g
    const matches = [...query.matchAll(mentionRegex)]
    const ids = matches.map(m => m[2])
    
    // 只有当解析出的 ids 与当前不同时才更新
    if (ids.length > 0 && JSON.stringify(ids) !== JSON.stringify(selectedSourceIds)) {
      setSelectedSourceIds(ids)
    }
  }, [query, selectedSourceIds])

  // 自动滚动到最新消息
  useEffect(() => {
    if (hasStarted) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, hasStarted])

  // 🆕 处理引用点击
  const handleReferenceClick = (reference: Reference) => {
    // 使用引用中的 article_id 和 id
    if (reference.id && reference.article_id) {
      setSelectedArticleId(reference.article_id)
      setSelectedEventId(reference.id)
      setIsDetailDrawerOpen(true)
    }
  }

  const handleSubmit = async () => {
    const plainText = query.replace(/@\[([^\]]+)\]\(([^)]+)\)/g, '').trim()
    if (!plainText || selectedSourceIds.length === 0) return

      // 添加用户消息
      const userMessage: Message = {
        id: Date.now().toString(),
        role: 'user',
        content: plainText,
        sources: selectedSourceIds,
        timestamp: new Date(),
      }

      setMessages(prev => [...prev, userMessage])

      // 保留 mentions，清除其他文本
      const mentionsOnly = query.match(/@\[([^\]]+)\]\(([^)]+)\)/g)?.join(' ') || ''
      setQuery(mentionsOnly ? mentionsOnly + ' ' : '')

    // 创建AI消息（简洁初始状态）
    const assistantId = (Date.now() + 1).toString()
    const assistantMessage: Message = {
      id: assistantId,
      role: 'assistant',
      content: '',
      searchStatus: {
        status: 'searching',
        message: '正在分析问题...'
      },  // 🆕 添加初始状态，确保思考框立即显示
      thinkingSteps: [],
      reasoning: '',
      references: [],
      timestamp: new Date(),
      isStreaming: true,
    }

    setMessages(prev => [...prev, assistantMessage])
    setThinkingOpenStates(prev => ({ ...prev, [assistantId]: true }))  // 初始为展开

    // 流式调用AI
    try {
      for await (const chunk of apiClient.chatStream({
        query: plainText,
        source_config_ids: selectedSourceIds,
        mode,
        context: messages.slice(-10).map(m => ({
          role: m.role,
          content: m.content,
          sources: m.sources,
        })),
        params: {
          top_k: topK[0],
          result_style: resultStyle,
        }
      })) {
        // 更新AI消息
        setMessages(prev => prev.map(msg => {
          if (msg.id !== assistantId) return msg

          const updated = { ...msg }

          switch (chunk.type) {
            case 'search_status':
              // 搜索状态
              updated.searchStatus = {
                status: chunk.status,
                message: chunk.message,
                sourcesCount: chunk.sources_count,
                eventsCount: chunk.events_count,
                confidence: chunk.confidence
              }
              break

            case 'thinking_step':
              // 执行步骤：添加新步骤
              updated.thinkingSteps = [
                ...(updated.thinkingSteps || []),
                {
                  stage: chunk.stage,
                  label: chunk.label,
                  content: chunk.content,
                  status: chunk.status
                }
              ]
              break

            case 'update_step':
              // 更新已有步骤
              updated.thinkingSteps = (updated.thinkingSteps || []).map(step =>
                step.stage === chunk.stage
                  ? { ...step, content: chunk.content, status: chunk.status }
                  : step
              )
              break

            case 'references':
              // 引用来源
              updated.references = chunk.data
              break

            case 'reasoning':
              // 实时推理：累积
              updated.reasoning = (updated.reasoning || '') + chunk.content
              break

            case 'content':
              // 回答内容（逐字追加）
              updated.content = updated.content + chunk.content
              break

            case 'done':
              // 完成
              updated.isStreaming = false
              updated.stats = chunk.stats
              // ✅ 保留 reasoning，不删除（UI会自动收起）
              
              // 🆕 延迟自动收起思考框
              setTimeout(() => {
                setThinkingOpenStates(prev => ({ ...prev, [assistantId]: false }))
              }, 800)  // 停留0.8秒让用户看到完成状态
              break

            case 'error':
              // 错误
              updated.content = `❌ ${chunk.content}`
              updated.isStreaming = false
              break
          }

          return updated
        }))
      }
    } catch (error) {
      console.error('Chat stream error:', error)
      // 更新为错误消息
      const errorMsg = error instanceof Error ? error.message : '未知错误'
      setMessages(prev => prev.map(msg =>
        msg.id === assistantId
          ? {
              ...msg,
              content: `❌ 错误：${errorMsg}`,
              isStreaming: false
            }
          : msg
      ))
    }
  }

  const modeConfig = {
    quick: { label: '快速', icon: Zap, desc: '快速回答，简洁明了' },
    deep: { label: '深入', icon: FileText, desc: '深度分析，详细解答' }
  }

  const CurrentIcon = modeConfig[mode].icon
  const hasContent = query.trim().length > 0
  const plainText = query.replace(/@\[([^\]]+)\]\(([^)]+)\)/g, '').trim()
  const canSubmit = plainText.length > 0

  // react-mentions 样式（紫色主题 - 多行模式）
  const mentionStyle = {
    control: {
      backgroundColor: '#fff',
      fontSize: 15,
      fontWeight: 400,
      minHeight: 48,
      maxHeight: 200,
    },
    '&multiLine': {
      control: {
        minHeight: 48,
        maxHeight: 200,
      },
      highlighter: {
        padding: '14px 50px 14px 14px',
        border: 0,
        lineHeight: '1.5',
        whiteSpace: 'pre-wrap',
      },
      input: {
        padding: '14px 50px 14px 14px',
        border: 0,
        outline: 0,
        color: '#374151',
        lineHeight: '1.5',
        whiteSpace: 'pre-wrap',
      },
    },
    suggestions: {
      list: {
        backgroundColor: 'white',
        border: '1px solid #e5e7eb',
        borderRadius: '12px',
        boxShadow: '0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)',
        fontSize: 14,
        overflow: 'hidden',
        position: 'fixed' as const,
        zIndex: 9999,
      },
      item: {
        padding: '12px 14px',
        borderBottom: '1px solid #f3f4f6',
        '&focused': {
          backgroundColor: '#f5f3ff',
        },
      },
    },
  }

  // 渲染输入框组件（复用）
  const renderInput = () => (
    <motion.div
      layout
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        duration: 0.6,
        delay: hasStarted ? 0 : 0.4,
        ease: [0.4, 0, 0.2, 1]
      }}
    >
      <div className="mx-auto max-w-3xl">
        <div className="flex items-end gap-0 border border-gray-200 rounded-lg bg-white shadow-sm hover:shadow-md focus-within:border-purple-400 focus-within:ring-2 focus-within:ring-purple-100 transition-all overflow-visible">
          {/* 左侧：对话模式 */}
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
              <DropdownMenuRadioGroup value={mode} onValueChange={(v) => setMode(v as 'quick' | 'deep')}>
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

          {/* 输入框 - react-mentions */}
          <div ref={inputContainerRef} className="flex-1 relative">
            <MentionsInput
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="输入 @ 选择信息源，然后提问..."
              style={mentionStyle}
              onKeyDown={(e: React.KeyboardEvent) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  handleSubmit()
                }
                // Shift+Enter 允许换行
              }}
            >
              <Mention
                trigger="@"
                data={sourcesData?.data?.map((s: { id: string; name: string }) => ({
                  id: s.id,
                  display: s.name,
                })) || []}
                renderSuggestion={(
                  suggestion: { id: string; display: string },
                  search: string,
                  highlightedDisplay: React.ReactNode
                ) => (
                  <div className="flex items-center gap-2">
                    <Database className="h-4 w-4 text-purple-600" />
                    <div>
                      <div className="font-medium">{highlightedDisplay}</div>
                      {sourcesData?.data?.find((s: { id: string; description?: string }) => s.id === suggestion.id)?.description && (
                        <div className="text-xs text-muted-foreground">
                          {sourcesData.data.find((s: { id: string; description?: string }) => s.id === suggestion.id)?.description}
                        </div>
                      )}
                    </div>
                  </div>
                )}
                markup="@[__display__](__id__)"
                displayTransform={(id: string, display: string) => `@${display}`}
                appendSpaceOnAdd
                style={{
                  backgroundColor: '#f3e8ff',
                  color: '#6b21a8',
                  fontWeight: 600,
                  opacity: 1,
                  position: 'relative',
                  zIndex: 2,
                }}
              />
            </MentionsInput>

            {/* 清空按钮 */}
            {hasContent && (
              <button
                onClick={() => setQuery('')}
                className="absolute right-2 bottom-3 h-6 w-6 rounded-full bg-gray-200 hover:bg-gray-300 flex items-center justify-center transition-colors z-10"
              >
                <X className="h-3.5 w-3.5 text-gray-600" />
              </button>
            )}
          </div>

          {/* 右侧：配置 + 发送 */}
          <div className="flex items-end shrink-0">
            {/* AI参数设置 */}
            <Popover open={showSettings} onOpenChange={setShowSettings}>
              <PopoverTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-12 w-10 p-0 rounded-none border-l hover:bg-gray-50"
                >
                  <Settings2 className="h-4 w-4 text-muted-foreground" />
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-80" align="end">
                <div className="space-y-4">
                  <h4 className="font-medium text-sm">AI 参数</h4>

                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <Label className="text-xs text-muted-foreground">检索内容条数</Label>
                      <span className="text-xs font-medium">{topK[0]}</span>
                    </div>
                    <Slider value={topK} onValueChange={setTopK} min={1} max={20} step={1} />
                  </div>

                  <div className="space-y-2">
                    <Label className="text-xs text-muted-foreground">结果风格</Label>
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant={resultStyle === 'concise' ? 'default' : 'outline'}
                        onClick={() => setResultStyle('concise')}
                        className="flex-1"
                      >
                        简洁
                      </Button>
                      <Button
                        size="sm"
                        variant={resultStyle === 'detailed' ? 'default' : 'outline'}
                        onClick={() => setResultStyle('detailed')}
                        className="flex-1"
                      >
                        详细
                      </Button>
                    </div>
                  </div>
                </div>
              </PopoverContent>
            </Popover>

            {/* 发送按钮 */}
            <Button
              onClick={handleSubmit}
              disabled={!canSubmit}
              size="lg"
              className={cn(
                "h-12 px-4 md:px-5 rounded-lg font-medium transition-all",
                hasContent
                  ? "bg-purple-600 hover:bg-purple-700 text-white"
                  : "bg-gray-100 text-gray-400 cursor-not-allowed"
              )}
            >
              <Send className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* 提示 */}
        {!hasStarted && (
          <p className="text-center text-xs text-muted-foreground mt-3">
            输入 <kbd className="px-1.5 py-0.5 bg-muted rounded text-xs">@</kbd> 选择信息源，
            <kbd className="px-1.5 py-0.5 bg-muted rounded text-xs mx-1">Shift+Enter</kbd> 换行，
            <kbd className="px-1.5 py-0.5 bg-muted rounded text-xs">Enter</kbd> 发送
          </p>
        )}
      </div>
    </motion.div>
  )

  return (
    // 填充 layout 提供的空间
    <div className="h-full flex flex-col overflow-hidden">
      <AnimatePresence mode="wait">
        {/* 未开始：输入框居中显示 */}
        {!hasStarted ? (
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
                <div className="text-center space-y-8 mb-16">
                  {/* Icon */}
                  <motion.div
                    initial={{ opacity: 0, scale: 0.8 }}
                    animate={{ opacity: 1, scale: 1 }}
                    transition={{ duration: 0.5, delay: 0.2 }}
                  >
                    <div className="flex justify-center">
                      <div className="p-6 rounded-2xl bg-linear-to-br from-violet-100 to-purple-100 shadow-lg">
                        <MessageSquare className="w-16 h-16 text-violet-600" strokeWidth={1.5} />
                      </div>
                    </div>
                  </motion.div>

                  {/* Title */}
                  <motion.div
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.5, delay: 0.3 }}
                  >
                    <h1 className="text-4xl font-bold text-gray-800 mb-3">AI 问答</h1>
                    <p className="text-lg text-gray-500 max-w-xl mx-auto leading-relaxed">
                      智能查询文档聚合和知识问答，让信息处理更智能
                    </p>
                  </motion.div>
                </div>
              </motion.div>

              {/* 输入框 */}
              <div className="px-4">
                {renderInput()}
              </div>
            </div>
          </motion.div>
        ) : (
          /* 开始对话：消息列表 + 输入框 */
          <motion.div
            key="chat"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.4 }}
            style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
          >
            {/* 消息列表 - flex-1 自动填充，内部滚动 */}
            <div className="flex-1 overflow-y-auto">
              <div className="max-w-4xl mx-auto pb-4 py-8">
                <AnimatePresence>
                  {messages.map((message, index) => (
                    <motion.div
                      key={message.id}
                      initial={{ opacity: 0, y: 20 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: 0.3, delay: index * 0.05 }}
                    >
                      <div className={cn(
                        "mb-6 flex gap-3",
                        message.role === 'user' ? 'justify-end' : 'justify-start'
                      )}>
                        {message.role === 'assistant' && (
                          <div className="w-8 h-8 rounded-full bg-purple-100 flex items-center justify-center shrink-0">
                            <Bot className="w-5 h-5 text-purple-600" />
                          </div>
                        )}

                        <div className={cn(
                          "max-w-[70%] rounded-lg px-4 py-3",
                          message.role === 'user'
                            ? 'bg-purple-600 text-white'
                            : 'bg-gray-100 text-gray-800'
                        )}>
                          {/* 用户消息的信息源标签 */}
                          {message.role === 'user' && message.sources && message.sources.length > 0 && (
                            <div className="flex gap-1 mb-2 flex-wrap">
                              {message.sources.map(sourceId => {
                                const source = sourcesData?.data?.find((s: { id: string; name: string }) => s.id === sourceId)
                                return source ? (
                                  <span key={sourceId} className="text-xs px-2 py-0.5 rounded bg-purple-500/20 text-purple-100">
                                    @{source.name}
                                  </span>
                                ) : null
                              })}
                            </div>
                          )}

                          {/* 统一的思考框 - 有内容时才显示 */}
                          {message.role === 'assistant' && ((message.thinkingSteps && message.thinkingSteps.length > 0) || message.reasoning) && (
                            <Collapsible 
                              open={thinkingOpenStates[message.id] ?? message.isStreaming}  // 🆕 受控组件
                              onOpenChange={(open) => {
                                setThinkingOpenStates(prev => ({ ...prev, [message.id]: open }))
                              }}
                              className="mb-3"
                            >
                              <CollapsibleTrigger className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-700 transition-colors group">
                                <Brain className="w-3.5 h-3.5" />
                                <span>
                                  {message.isStreaming ? (
                                    '思考中...'
                                  ) : (
                                    `查看思考过程${message.stats?.thinking_time ? ` (${message.stats.thinking_time}秒)` : ''}`
                                  )}
                                </span>
                                {message.isStreaming ? (
                                  <Loader2 className="w-3 h-3 animate-spin text-blue-500" />
                                ) : (
                                  <ChevronDown className="w-3 h-3 transition-transform group-data-[state=open]:rotate-180" />
                                )}
                              </CollapsibleTrigger>
                              
                              <CollapsibleContent className="mt-2 p-3 bg-gray-50/50 rounded-lg border border-gray-200 max-h-80">
                                {/* 执行步骤 - 简洁显示 */}
                                {message.thinkingSteps && message.thinkingSteps.length > 0 && (
                                  <div className="space-y-1 mb-3 pb-3 border-b border-gray-200">
                                    {message.thinkingSteps.map((step, i) => (
                                      <div key={i} className="flex items-center gap-2 text-xs">
                                        <span className={cn(
                                          step.status === 'done' && "text-green-600",
                                          step.status === 'processing' && "text-blue-600"
                                        )}>
                                          {step.status === 'done' ? '✓' : '⏳'}
                                        </span>
                                        <span className="text-gray-700">
                                          <span className="font-medium">{step.label}</span>
                                          <span className="text-gray-400 mx-1.5">→</span>
                                          <span className={cn(
                                            "text-gray-600",
                                            step.status === 'processing' && "animate-pulse"
                                          )}>{step.content}</span>
                                        </span>
                                      </div>
                                    ))}
                                  </div>
                                )}
                                
                                {/* 思考过程内容 */}
                                {message.reasoning && (
                                  <ReasoningContent 
                                    content={message.reasoning}
                                    isStreaming={message.isStreaming}
                                  />
                                )}
                              </CollapsibleContent>
                            </Collapsible>
                          )}

                          {/* 3. 答案内容 - 支持markdown和引用 */}
                          <div className="text-sm leading-relaxed prose prose-sm max-w-none">
                            <MessageContent 
                              content={message.content}
                              references={message.references}
                              onReferenceClick={(refIndex) => {
                                const ref = message.references?.[refIndex]
                                if (ref) {
                                  handleReferenceClick(ref)
                                }
                              }}
                            />
                          </div>

                          {/* 4. 引用来源（卡片式） - 只显示正文中实际引用的 */}
                          {message.role === 'assistant' && message.references && message.references.length > 0 && !message.isStreaming && (() => {
                            // 从回答内容中提取实际引用的序号
                            const citedNumbers = new Set<number>()
                            const matches = message.content.matchAll(/\[#(\d+)\]/g)
                            for (const match of matches) {
                              citedNumbers.add(parseInt(match[1]))
                            }
                            
                            // 过滤出实际被引用的事项
                            const citedReferences = message.references
                              .map((ref, index) => ({ ref, displayIndex: index + 1 }))
                              .filter(({ displayIndex }) => citedNumbers.has(displayIndex))
                            
                            if (citedReferences.length === 0) return null
                            
                            return (
                            <div className="mt-4 pt-4 border-t border-gray-200 space-y-2">
                              <div className="text-xs font-medium text-gray-700 mb-2">
                                  📚 参考来源 ({citedReferences.length})
                              </div>
                                {citedReferences.map(({ ref, displayIndex }) => (
                                <div
                                    key={ref.id}
                                    onClick={() => handleReferenceClick(ref)}
                                  className="p-2.5 bg-white/60 rounded border border-gray-200 hover:border-purple-300 hover:shadow-sm transition-all cursor-pointer"
                                >
                                  <div className="text-xs font-medium text-gray-800 flex items-start gap-2">
                                      <span className="inline-flex items-center justify-center min-w-[20px] h-5 px-1.5 text-[10px] font-medium text-purple-600 bg-purple-100 rounded shrink-0 hover:bg-purple-200">
                                        #{displayIndex}
                                    </span>
                                    <span className="flex-1">{ref.title}</span>
                                  </div>
                                  {ref.summary && (
                                      <div className="text-xs text-gray-600 mt-1.5 ml-7 line-clamp-2">
                                      {ref.summary}
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                            )
                          })()}
                        </div>

                        {message.role === 'user' && (
                          <div className="w-8 h-8 rounded-full bg-blue-100 flex items-center justify-center shrink-0">
                            <User className="w-5 h-5 text-blue-600" />
                          </div>
                        )}
                      </div>
                    </motion.div>
                  ))}
                </AnimatePresence>
                <div ref={messagesEndRef} />
              </div>
            </div>

            {/* 输入框 - 从上方下沉到底部 */}
            <motion.div
              initial={{ y: -300, opacity: 0.5 }}
              animate={{ y: 0, opacity: 1 }}
              transition={{
                duration: 0.8,
                ease: [0.34, 1.05, 0.64, 1]  // 轻微弹性缓动
              }}
            >
              <div className="px-4 py-4">
                {renderInput()}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* 添加全局样式 - 多行模式 */}
      <style jsx global>{`
        .mentions__control {
          min-width: 100% !important;
          width: 100% !important;
        }

        .mentions__input {
          border: none !important;
          outline: none !important;
          color: #374151 !important;
          caret-color: #000 !important;
          line-height: 1.5 !important;
          white-space: pre-wrap !important;
          resize: none !important;
          max-height: 200px !important;
          overflow-y: auto !important;
        }

        .mentions__highlighter {
          border: none !important;
          padding: 14px 50px 14px 14px !important;
          line-height: 1.5 !important;
          white-space: pre-wrap !important;
          max-height: 200px !important;
          overflow-y: auto !important;
        }

        .mentions__mention {
          background-color: #f3e8ff !important;
          color: #6b21a8 !important;
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
          background-color: #f5f3ff !important;
        }

        .scrollbar-hide {
          -ms-overflow-style: none;
          scrollbar-width: none;
        }

        .scrollbar-hide::-webkit-scrollbar {
          display: none;
        }
      `}</style>

      {/* 文档详情抽屉 - 与搜索页面一致 */}
      <DocumentDetailDrawer
        open={isDetailDrawerOpen}
        onClose={() => setIsDetailDrawerOpen(false)}
        articleId={selectedArticleId || undefined}
        defaultView={detailDrawerView}
        highlightEventId={selectedEventId || undefined}
      />
    </div>
  )
}

// 消息内容组件（支持markdown和引用）
function MessageContent({ 
  content, 
  references, 
  onReferenceClick 
}: { 
  content: string
  references?: Reference[]
  onReferenceClick?: (refIndex: number) => void
}) {
  // 预处理引用标志为 JSX
  const processContent = (text: string) => {
    const parts = text.split(/(\[#\d+\])/g)
    
    return (
      <>
        {parts.map((part, i) => {
          if (/^\[#\d+\]$/.test(part)) {
            const num = parseInt(part.match(/\d+/)?.[0] || '0')
            return (
              <span
                key={i}
                onClick={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  // 触发引用点击
                  if (onReferenceClick && num > 0 && references && references[num - 1]) {
                    onReferenceClick(num - 1)
                  }
                }}
                className="inline-flex items-center justify-center w-5 h-5 text-[10px] font-medium text-purple-600 bg-purple-100 rounded hover:bg-purple-200 transition-colors cursor-pointer mx-0.5"
                title={`查看来源 ${num}`}
              >
                {num}
              </span>
            )
          }
          return <span key={i}>{part}</span>
        })}
      </>
    )
  }
  
  return (
    <ReactMarkdown
      components={{
        // 段落 - 处理其中的引用
        p: ({ children }) => <p className="mb-2">{processChildren(children)}</p>,
        strong: ({ children }) => <strong className="font-semibold text-gray-900">{processChildren(children)}</strong>,
        h1: ({ children }) => <h1 className="text-xl font-bold mt-4 mb-2">{children}</h1>,
        h2: ({ children }) => <h2 className="text-lg font-semibold mt-3 mb-2">{children}</h2>,
        h3: ({ children }) => <h3 className="text-base font-semibold mt-2 mb-1">{children}</h3>,
        ul: ({ children }) => <ul className="list-disc ml-4 mb-2 space-y-1">{children}</ul>,
        li: ({ children }) => <li className="text-gray-700">{processChildren(children)}</li>,
        code: ({ children }) => <code className="px-1 py-0.5 bg-gray-100 rounded text-xs font-mono">{children}</code>,
        // 表格支持
        table: ({ children }) => (
          <div className="overflow-x-auto my-2">
            <table className="border-collapse border border-gray-300 min-w-full">{children}</table>
          </div>
        ),
        thead: ({ children }) => <thead className="bg-gray-100">{children}</thead>,
        tr: ({ children }) => <tr className="border-b border-gray-200">{children}</tr>,
        th: ({ children }) => <th className="border border-gray-300 px-3 py-2 text-left font-semibold text-sm">{children}</th>,
        td: ({ children }) => <td className="border border-gray-300 px-3 py-2 text-sm">{children}</td>,
      }}
    >
      {content}
    </ReactMarkdown>
  )
  
  // 处理 children（可能包含字符串和React节点）
  function processChildren(children: React.ReactNode): React.ReactNode {
    if (typeof children === 'string') {
      return processContent(children)
    }
    if (Array.isArray(children)) {
      return children.map((child, i) => 
        typeof child === 'string' ? <span key={i}>{processContent(child)}</span> : child
      )
    }
    return children
  }
}

// 思考过程内容组件（带自动滚动）
function ReasoningContent({ 
  content, 
  isStreaming
}: { 
  content: string
  isStreaming?: boolean
}) {
  const reasoningRef = useRef<HTMLDivElement>(null)
  
  // 自动滚动到底部
  useEffect(() => {
    if (reasoningRef.current && isStreaming) {
      reasoningRef.current.scrollTop = reasoningRef.current.scrollHeight
    }
  }, [content, isStreaming])
  
  return (
    <div 
      ref={reasoningRef}
      className="text-xs text-gray-600 leading-relaxed whitespace-pre-wrap max-h-48 overflow-y-auto scrollbar-thin"
    >
      {content}
      {isStreaming && (
        <span className="inline-block w-1 h-3 bg-blue-600 animate-pulse ml-0.5" />
      )}
    </div>
  )
}

// Next.js 页面默认导出
export default function ChatPage() {
  return (
    <Suspense fallback={<div className="flex items-center justify-center h-screen"><Loader2 className="w-8 h-8 animate-spin text-purple-600" /></div>}>
      <ChatPageContent />
    </Suspense>
  )
}
