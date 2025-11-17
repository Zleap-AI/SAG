'use client'

import { Sparkles, Layers, Zap } from 'lucide-react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  SelectGroup,
  SelectLabel,
  SelectSeparator,
} from '@/components/ui/select'

// FilterType 格式: 'all' | 'llm' | 'llm:scenario' | 'embedding' | 'embedding:scenario'
export type ModelFilterType = string

interface ModelConfigFilterProps {
  value: ModelFilterType
  onChange: (value: ModelFilterType) => void
  counts?: {
    all: number
    llm: number
    'llm:general': number
    'llm:extract': number
    'llm:search': number
    'llm:chat': number
    'llm:summary': number
    embedding: number
    'embedding:general': number
  }
}

const SCENARIO_LABELS: Record<string, string> = {
  general: '通用场景',
  extract: '数据提取',
  search: '智能搜索',
  chat: '对话交互',
  summary: '内容摘要',
}

export function ModelConfigFilter({ value, onChange, counts }: ModelConfigFilterProps) {
  // 获取当前选中项的显示文本
  const getDisplayText = () => {
    const count = counts?.[value as keyof typeof counts] || 0

    if (value === 'all') return `全部类型 (${count})`
    if (value === 'llm') return `LLM 模型 (${count})`
    if (value === 'embedding') return `Embedding 模型 (${count})`

    // 处理场景筛选 'llm:general'
    if (value.includes(':')) {
      const [type, scenario] = value.split(':')
      return `${type === 'llm' ? 'LLM' : 'Embedding'} - ${SCENARIO_LABELS[scenario]} (${count})`
    }

    return '选择筛选条件'
  }

  // 获取图标颜色
  const getIconColor = (type: string) => {
    if (type === 'all') return 'text-gray-400'
    if (type.startsWith('llm')) return 'text-purple-500'
    if (type.startsWith('embedding')) return 'text-blue-500'
    return 'text-gray-500'
  }

  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-[240px] bg-white border-gray-200 hover:border-gray-300 focus:ring-blue-500 transition-all">
        <SelectValue>
          <div className="flex items-center gap-2">
            {value === 'all' && <Layers className={`w-4 h-4 ${getIconColor('all')}`} />}
            {(value === 'llm' || value.startsWith('llm:')) && <Sparkles className={`w-4 h-4 ${getIconColor('llm')}`} />}
            {(value === 'embedding' || value.startsWith('embedding:')) && <Zap className={`w-4 h-4 ${getIconColor('embedding')}`} />}
            <span className="text-sm">{getDisplayText()}</span>
          </div>
        </SelectValue>
      </SelectTrigger>
      
      <SelectContent className="bg-white border-gray-200 shadow-lg">
        {/* 全部类型 */}
        <SelectItem value="all" className="cursor-pointer">
          <div className="flex items-center gap-2">
            <Layers className={`w-4 h-4 ${getIconColor('all')}`} />
            <span>全部类型</span>
            {counts?.all !== undefined && (
              <span className="ml-auto text-xs text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">
                {counts.all}
              </span>
            )}
          </div>
        </SelectItem>

        {/* LLM 模型 */}
        {(counts?.llm ?? 0) > 0 && (
          <>
            <SelectSeparator className="my-1" />
            
            {/* LLM 总计 */}
            <SelectItem value="llm" className="cursor-pointer">
              <div className="flex items-center gap-2">
                <Sparkles className={`w-4 h-4 ${getIconColor('llm')}`} />
                <span>LLM 模型</span>
                <span className="ml-auto text-xs text-gray-500 bg-purple-50 px-2 py-0.5 rounded-full">
                  {counts?.llm ?? 0}
                </span>
              </div>
            </SelectItem>

            {/* LLM 场景细分 - 缩进显示 */}
            {['general', 'extract', 'search', 'chat', 'summary'].map((scenario) => {
              const key = `llm:${scenario}` as keyof typeof counts
              const count = counts?.[key] ?? 0
              if (count === 0) return null
              
              return (
                <SelectItem key={key} value={key} className="cursor-pointer pl-10">
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-gray-600">{SCENARIO_LABELS[scenario]}</span>
                    <span className="ml-auto text-xs text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">
                      {count}
                    </span>
                  </div>
                </SelectItem>
              )
            })}
          </>
        )}

        {/* Embedding 模型 */}
        {(counts?.embedding ?? 0) > 0 && (
          <>
            <SelectSeparator className="my-1" />
            
            {/* Embedding 总计 */}
            <SelectItem value="embedding" className="cursor-pointer">
              <div className="flex items-center gap-2">
                <Zap className={`w-4 h-4 ${getIconColor('embedding')}`} />
                <span>Embedding 模型</span>
                <span className="ml-auto text-xs text-gray-500 bg-blue-50 px-2 py-0.5 rounded-full">
                  {counts?.embedding ?? 0}
                </span>
              </div>
            </SelectItem>

            {/* Embedding 场景细分 - 缩进显示 */}
            {(() => {
              const count = counts?.['embedding:general'] ?? 0
              if (count === 0) return null
              
              return (
                <SelectItem value="embedding:general" className="cursor-pointer pl-10">
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-gray-600">{SCENARIO_LABELS.general}</span>
                    <span className="ml-auto text-xs text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">
                      {count}
                    </span>
                  </div>
                </SelectItem>
              )
            })()}
          </>
        )}
      </SelectContent>
    </Select>
  )
}

