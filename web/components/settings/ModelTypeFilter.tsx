'use client'

import { Layers, Cpu, Binary } from 'lucide-react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  SelectGroup,
  SelectLabel,
} from '@/components/ui/select'

export type ModelType = 'all' | 'llm' | 'embedding'

interface ModelTypeFilterProps {
  value: ModelType
  onChange: (value: ModelType) => void
  counts: {
    all: number
    llm: number
    embedding: number
  }
}

const MODEL_TYPE_OPTIONS: Array<{
  value: ModelType
  label: string
  icon: any
  color: string
}> = [
  { value: 'all', label: '全部类型', icon: Layers, color: 'text-gray-500' },
  { value: 'llm', label: 'LLM 模型', icon: Cpu, color: 'text-purple-600' },
  { value: 'embedding', label: 'Embedding 模型', icon: Binary, color: 'text-blue-600' },
]

export function ModelTypeFilter({ value, onChange, counts }: ModelTypeFilterProps) {
  const currentOption = MODEL_TYPE_OPTIONS.find(opt => opt.value === value) || MODEL_TYPE_OPTIONS[0]
  const Icon = currentOption.icon
  const count = counts[value] || 0

  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-[200px] bg-white border-gray-200 hover:border-gray-300 focus:ring-purple-400 transition-all">
        <SelectValue>
          <div className="flex items-center gap-2">
            <Icon className={`w-4 h-4 ${currentOption.color}`} />
            <span className="text-sm">{currentOption.label} ({count})</span>
          </div>
        </SelectValue>
      </SelectTrigger>
      
      <SelectContent className="bg-white border-gray-200 shadow-lg">
        <SelectGroup>
          <SelectLabel className="text-xs text-gray-500 font-semibold">模型类型</SelectLabel>
          {MODEL_TYPE_OPTIONS.map((option) => {
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

