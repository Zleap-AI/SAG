'use client'

import { Layers, FileText, Search, MessageSquare, Sparkles, Cpu } from 'lucide-react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  SelectGroup,
  SelectLabel,
} from '@/components/ui/select'

export type ScenarioType = 'all' | 'extract' | 'search' | 'chat' | 'summary' | 'general'

interface ScenarioFilterProps {
  value: ScenarioType
  onChange: (value: ScenarioType) => void
  counts: {
    all: number
    extract: number
    search: number
    chat: number
    summary: number
    general: number
  }
}

const SCENARIO_OPTIONS: Array<{
  value: ScenarioType
  label: string
  icon: any
  color: string
}> = [
  { value: 'all', label: '全部场景', icon: Layers, color: 'text-gray-500' },
  { value: 'extract', label: '数据提取', icon: FileText, color: 'text-emerald-600' },
  { value: 'search', label: '智能搜索', icon: Search, color: 'text-blue-600' },
  { value: 'chat', label: '对话交互', icon: MessageSquare, color: 'text-purple-600' },
  { value: 'summary', label: '内容摘要', icon: Sparkles, color: 'text-pink-600' },
  { value: 'general', label: '通用场景', icon: Cpu, color: 'text-indigo-600' },
]

export function ScenarioFilter({ value, onChange, counts }: ScenarioFilterProps) {
  const currentOption = SCENARIO_OPTIONS.find(opt => opt.value === value) || SCENARIO_OPTIONS[0]
  const Icon = currentOption.icon
  const count = counts[value] || 0

  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-[240px] bg-white border-gray-200 hover:border-gray-300 focus:ring-indigo-400 transition-all">
        <SelectValue>
          <div className="flex items-center gap-2">
            <Icon className={`w-4 h-4 ${currentOption.color}`} />
            <span className="text-sm">{currentOption.label} ({count})</span>
          </div>
        </SelectValue>
      </SelectTrigger>
      
      <SelectContent className="bg-white border-gray-200 shadow-lg">
        <SelectGroup>
          <SelectLabel className="text-xs text-gray-500 font-semibold">应用场景</SelectLabel>
          {SCENARIO_OPTIONS.map((option) => {
            const OptionIcon = option.icon
            const optionCount = counts[option.value]
            return (
              <SelectItem key={option.value} value={option.value} className="cursor-pointer">
                <div className="flex items-center gap-2">
                  <OptionIcon className={`w-4 h-4 ${option.color}`} />
                  <span>{option.label}</span>
                  {optionCount !== undefined && (
                    <span className="ml-auto text-xs text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">
                      {optionCount}
                    </span>
                  )}
                </div>
              </SelectItem>
            )
          })}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}

