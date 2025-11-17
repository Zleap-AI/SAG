'use client'

import { useState } from 'react'
import { motion } from 'framer-motion'
import {
  Edit2,
  Trash2,
  MoreVertical,
  Sparkles as SparklesIcon,
  Zap,
  Clock,
  FileText,
  Search,
  MessageSquare,
  Cpu,
  Award,
  Globe,
} from 'lucide-react'
import { ModelConfig } from '@/types'
import { Switch } from '@/components/ui/switch'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

interface ModelConfigCardProps {
  config: ModelConfig
  onEdit: (config: ModelConfig) => void
  onDelete: (config: ModelConfig) => void
  onToggle: (config: ModelConfig, isActive: boolean) => void
  index: number
}

const SCENARIO_ICONS: Record<string, any> = {
  extract: FileText,
  search: Search,
  chat: MessageSquare,
  summary: SparklesIcon,
  general: Globe,
}

const SCENARIO_LABELS: Record<string, string> = {
  extract: '数据提取',
  search: '智能搜索',
  chat: '对话交互',
  summary: '内容摘要',
  general: '通用场景',
}

const SCENARIO_COLORS: Record<string, { bg: string; text: string }> = {
  extract: { bg: 'bg-emerald-50', text: 'text-emerald-600' },
  search: { bg: 'bg-blue-50', text: 'text-blue-600' },
  chat: { bg: 'bg-purple-50', text: 'text-purple-600' },
  summary: { bg: 'bg-pink-50', text: 'text-pink-600' },
  general: { bg: 'bg-indigo-50', text: 'text-indigo-600' },
}

export function ModelConfigCard({ config, onEdit, onDelete, onToggle, index }: ModelConfigCardProps) {
  const [isToggling, setIsToggling] = useState(false)

  const handleToggle = async (checked: boolean) => {
    setIsToggling(true)
    try {
      await onToggle(config, checked)
    } finally {
      setIsToggling(false)
    }
  }

  const ScenarioIcon = SCENARIO_ICONS[config.scenario] || Cpu
  const scenarioColor = SCENARIO_COLORS[config.scenario] || SCENARIO_COLORS.general

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.95 }}
      transition={{ duration: 0.3, delay: index * 0.1 }}
      whileHover={{ y: -5 }}
      className="relative border-0 rounded-lg p-6 bg-white/80 backdrop-blur-sm shadow-lg hover:shadow-xl transition-all duration-300"
    >
      {/* 右上角：开关 + 菜单 */}
      <div className="absolute top-4 right-4 flex items-center gap-2">
        <Switch
          checked={config.is_active}
          onCheckedChange={handleToggle}
          disabled={isToggling}
          className="data-[state=checked]:bg-yellow-500"
        />

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              className="p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
              title="更多操作"
            >
              <MoreVertical className="w-4 h-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-32">
            <DropdownMenuItem
              onClick={() => onEdit(config)}
              className="cursor-pointer text-sm"
            >
              <Edit2 className="w-4 h-4 mr-2" />
              编辑
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={() => onDelete(config)}
              className="cursor-pointer text-sm text-red-600 focus:text-red-600"
            >
              <Trash2 className="w-4 h-4 mr-2" />
              删除
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* 垂直布局 */}
      <div className="space-y-4">
        {/* 顶部：场景图标 + 配置名称 */}
        <div className="flex items-start gap-3">
          <div className={`p-1.5 rounded-lg shrink-0 ${scenarioColor.bg}`}>
            <ScenarioIcon className={`w-4 h-4 ${scenarioColor.text}`} />
          </div>
          <div className="flex-1 min-w-0">
            {/* 标题 */}
            <h3 className="font-semibold text-lg text-gray-900 truncate">
              {config.name}
            </h3>
          </div>
        </div>
            
            {/* 描述 */}
            {config.description && (
              <p className="text-sm text-gray-600 line-clamp-2">
                {config.description}
              </p>
            )}
            
            {/* 供应商/类型/用途 */}
            <div className="flex items-center gap-2 flex-wrap">
              {/* 供应商 */}
              {config.provider && (
                <span className="px-2 py-1 bg-indigo-50 text-indigo-700 rounded text-xs font-medium">
                  {config.provider === '302ai' ? '302.AI' : config.provider}
                </span>
              )}
              
              {/* 类型徽章 */}
              <span className={`inline-flex items-center px-2 py-1 rounded text-xs font-medium ${
                config.type === 'llm' 
                  ? 'bg-purple-100 text-purple-700' 
                  : 'bg-blue-100 text-blue-700'
              }`}>
                {config.type === 'llm' ? 'LLM' : 'Embedding'}
              </span>
              
              {/* 场景 */}
              <span className="text-xs text-gray-500">
                {SCENARIO_LABELS[config.scenario] || config.scenario}
              </span>
              
              {/* Embedding 维度 */}
              {config.type === 'embedding' && config.extra_data?.dimensions && (
                <span className="text-xs text-gray-400">
                  • {config.extra_data.dimensions}维
                </span>
              )}
              
              {/* 优先级 */}
              {config.priority > 0 && (
                <span className="px-2 py-1 bg-amber-50 text-amber-700 rounded text-xs font-medium flex items-center gap-1">
                  <Award className="w-3 h-3" />
                  {config.priority}
                </span>
              )}
        </div>

        {/* 模型名称 - 独立一行 */}
        <div className="text-sm text-gray-700 font-mono bg-gray-50 px-3 py-2 rounded truncate">
          {config.model}
        </div>

        {/* 参数信息 */}
        <div className="space-y-2 text-sm pt-3 border-t border-gray-200">
          <div className="flex items-center justify-between">
            <span className="text-gray-500 flex items-center gap-1">
              <Zap className="w-3.5 h-3.5" />
              温度:
            </span>
            <span className="font-medium text-gray-700">{config.temperature}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-gray-500 flex items-center gap-1">
              <SparklesIcon className="w-3.5 h-3.5" />
              Token:
            </span>
            <span className="font-medium text-gray-700">
              {config.max_tokens >= 1000
                ? `${(config.max_tokens / 1000).toFixed(0)}K`
                : config.max_tokens}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-gray-500 flex items-center gap-1">
              <Clock className="w-3.5 h-3.5" />
              超时:
            </span>
            <span className="font-medium text-gray-700">{config.timeout}s</span>
          </div>
        </div>
      </div>
    </motion.div>
  )
}

