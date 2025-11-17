'use client'

import React, { useState, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Sparkles, Loader2, CheckCircle2, XCircle, ChevronDown, ChevronUp } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { cn } from '@/lib/utils'

// 获取 API 基础 URL（与 api-client.ts 保持一致）
const getApiBaseUrl = () => {
  if (typeof window === 'undefined') return ''
  // 如果明确设置了 NEXT_PUBLIC_API_URL，使用它（本地开发）
  if (process.env.NEXT_PUBLIC_API_URL) {
    return process.env.NEXT_PUBLIC_API_URL
  }
  // 否则使用相对路径（Docker/生产环境，走Nginx代理）
  return ''
}

const getSummarizeUrl = () => {
  const baseUrl = getApiBaseUrl()
  return `${baseUrl}/api/v1/pipeline/summarize`
}

interface AISummaryProps {
  sourceId: string
  query: string
  eventIds: string[]
  onReferenceClick?: (eventId: string) => void
  className?: string
}

export function AISummary({ sourceId, query, eventIds, onReferenceClick, className }: AISummaryProps) {
  const [thinking, setThinking] = useState('')
  const [content, setContent] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [isDone, setIsDone] = useState(false)
  const [error, setError] = useState('')
  const [showThinkingDetail, setShowThinkingDetail] = useState(true)  // 默认打开
  const [thinkingTime, setThinkingTime] = useState(0)
  
  const hasStartedRef = useRef<string>('')
  const thinkingStartTime = useRef<number>(0)

  // 内容开始时记录思考时间并收起
  useEffect(() => {
    if (content && thinkingTime === 0 && thinkingStartTime.current > 0) {
      setThinkingTime(Date.now() - thinkingStartTime.current)
      setShowThinkingDetail(false)  // 思考结束，自动收起
    }
  }, [content, thinkingTime])

  // 渲染引用
  const renderContentWithReferences = (text: string) => {
    const parts = text.split(/(\[#\d+\])/g)
    
    return parts.map((part, index) => {
      const match = part.match(/\[#(\d+)\]/)
      if (match) {
        const order = match[1]
        return (
          <span
            key={index}
            className="inline-flex items-center justify-center min-w-[20px] h-5 px-1.5 mx-0.5 text-xs font-semibold rounded bg-green-100 text-green-700 hover:bg-green-200 cursor-pointer transition-colors"
            onClick={(e) => {
              e.stopPropagation()
              const eventId = eventIds[parseInt(order) - 1]
              if (eventId && onReferenceClick) {
                onReferenceClick(eventId)
              }
            }}
            title={`点击查看事项 #${order}`}
          >
            {order}
          </span>
        )
      }
      return <span key={index}>{part}</span>
    })
  }

  useEffect(() => {
    if (!eventIds || eventIds.length === 0) return
    
    // 只基于 eventIds 变化触发（搜索结果变化时）
    const key = eventIds.join(',')
    if (hasStartedRef.current === key) return
    hasStartedRef.current = key

    const startSummarize = async () => {
      setIsStreaming(true)
      setThinking('')
      setContent('')
      setError('')
      setIsDone(false)
      setShowThinkingDetail(true)  // 初始打开，显示思考
      setThinkingTime(0)
      thinkingStartTime.current = Date.now()

      try {
        const response = await fetch(getSummarizeUrl(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source_config_id: sourceId,
            query: query,
            event_ids: eventIds,
          }),
        })

        if (!response.ok) throw new Error('总结请求失败')

        const reader = response.body?.getReader()
        const decoder = new TextDecoder()
        if (!reader) throw new Error('无法读取响应流')

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          const chunk = decoder.decode(value, { stream: true })
          const lines = chunk.split('\n')

          for (let i = 0; i < lines.length; i++) {
            const line = lines[i].trim()
            
            if (line.startsWith('event: thinking')) {
              const dataLine = lines[i + 1]
              if (dataLine?.startsWith('data: ')) {
                setThinking(prev => prev + dataLine.substring(6))
              }
            } else if (line.startsWith('event: content')) {
              const dataLine = lines[i + 1]
              if (dataLine?.startsWith('data: ')) {
                setContent(prev => prev + dataLine.substring(6))
              }
            } else if (line.startsWith('event: error')) {
              const dataLine = lines[i + 1]
              if (dataLine?.startsWith('data: ')) {
                setError(dataLine.substring(6))
              }
            } else if (line.startsWith('event: done')) {
              setIsDone(true)
              setIsStreaming(false)
            }
          }
        }
      } catch (err: any) {
        setError(err.message || '总结失败')
        setIsStreaming(false)
      }
    }

    startSummarize()
  }, [eventIds])  // 只依赖 eventIds，搜索结果变化时才触发

  if (!eventIds || eventIds.length === 0) return null

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className={cn("mb-4", className)}
    >
      <Card className="border-gray-200 bg-gray-50">
        <div className="px-5 py-4">
          {/* 标题 */}
          <div className="flex items-center gap-2 mb-3">
            <Sparkles className="h-4 w-4 text-blue-600" />
            <span className="text-sm font-semibold text-gray-900">AI 智能总结</span>
            {isDone && <CheckCircle2 className="h-4 w-4 text-green-600 ml-auto" />}
            {isStreaming && !isDone && <Loader2 className="h-4 w-4 animate-spin text-yellow-500 ml-auto" />}
          </div>

          {/* 思考过程折叠 */}
          {thinking && (thinkingTime > 0 || isStreaming) && (
            <div className="mb-3">
              <button
                onClick={() => setShowThinkingDetail(!showThinkingDetail)}
                className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-700 transition-colors"
              >
                {showThinkingDetail ? (
                  <ChevronUp className="h-3.5 w-3.5" />
                ) : (
                  <ChevronDown className="h-3.5 w-3.5" />
                )}
                <span className="flex items-center gap-1">
                  {content ? '思考过程' : '思考中'}
                  {!content && isStreaming && (
                    <Loader2 className="h-3 w-3 animate-spin text-yellow-500" />
                  )}
                  {thinkingTime > 0 && ` (${(thinkingTime / 1000).toFixed(1)}s)`}
                </span>
              </button>

              {/* 思考内容 */}
              <AnimatePresence>
                {showThinkingDetail && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: "auto" }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.2 }}
                    className="mt-2"
                  >
                    <div className="text-xs text-gray-600 font-mono leading-relaxed whitespace-pre-wrap">
                      {thinking}
                      {isStreaming && !content && (
                        <span className="inline-block ml-0.5 animate-pulse text-yellow-600">▊</span>
                      )}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}

          {/* 正文 */}
          {content && (
            <div className="text-[15px] text-black leading-[1.7] whitespace-pre-wrap">
              {renderContentWithReferences(content)}
              {isStreaming && !isDone && (
                <span className="inline-block ml-1 w-0.5 h-4 bg-blue-600 animate-pulse align-middle" />
              )}
            </div>
          )}

          {/* 加载 */}
          {isStreaming && !thinking && !content && !error && (
            <div className="flex items-center gap-2 text-gray-600 py-4">
              <Loader2 className="h-4 w-4 animate-spin text-yellow-500" />
              <span className="text-sm">正在分析...</span>
            </div>
          )}

          {/* 错误 */}
          {error && (
            <div className="flex items-start gap-2 p-3 rounded bg-red-50 border border-red-200">
              <XCircle className="h-4 w-4 text-red-600 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-red-900">总结失败</p>
                <p className="text-xs text-red-700 mt-0.5">{error}</p>
              </div>
            </div>
          )}
        </div>
      </Card>
    </motion.div>
  )
}
