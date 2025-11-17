'use client'

import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Loader2, ChevronDown } from 'lucide-react'
import { ModelConfig } from '@/types'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

interface ModelConfigDialogProps {
  open: boolean
  onClose: () => void
  onSubmit: (data: ModelConfigFormData) => void
  config?: ModelConfig | null
  isLoading?: boolean
}

export interface ModelConfigFormData {
  name: string
  description?: string
  type: 'llm' | 'embedding' | 'rerank'  // 模型类型（暂时只支持 llm 和 embedding 的编辑）
  scenario: 'extract' | 'search' | 'chat' | 'summary' | 'general'
  provider?: string
  api_key: string
  base_url: string
  model: string
  // LLM 专用参数
  temperature: number
  max_tokens: number
  top_p: number
  frequency_penalty: number
  presence_penalty: number
  // 通用参数
  timeout: number
  max_retries: number
  is_active: boolean
  priority: number
  // Embedding 专用参数
  dimensions?: number
}

// 供应商预设配置 - 只保留 302.AI 和自定义
const PROVIDER_PRESETS: Record<string, { 
  base_url: string
  model: string
  label: string
}> = {
  '302ai': {
    base_url: 'https://api.302.ai',
    model: 'sophnet/Qwen3-30B-A3B-Thinking-2507',
    label: '302.AI'
  },
  'custom': {
    base_url: '',
    model: '',
    label: '自定义'
  }
}

export function ModelConfigDialog({
  open,
  onClose,
  onSubmit,
  config,
  isLoading = false,
}: ModelConfigDialogProps) {
  const isEditMode = !!config

  const [formData, setFormData] = useState<ModelConfigFormData>({
    name: '',
    description: '',
    type: 'llm',  // 🆕 默认 LLM
    scenario: 'general',
    provider: '302ai',
    api_key: '',
    base_url: 'https://api.302.ai',
    model: 'sophnet/Qwen3-30B-A3B-Thinking-2507',
    temperature: 0.7,
    max_tokens: 8000,
    top_p: 1.0,
    frequency_penalty: 0.0,
    presence_penalty: 0.0,
    timeout: 600,
    max_retries: 3,
    is_active: true,
    priority: 0,
    dimensions: undefined,
  })

  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [errors, setErrors] = useState<Partial<Record<keyof ModelConfigFormData, string>>>({})

  useEffect(() => {
    if (open) {
      if (config) {
        // 编辑模式：根据 base_url 推断 provider
        let detectedProvider = 'custom'
        if (config.base_url?.includes('302.ai')) {
          detectedProvider = '302ai'
        }

        setFormData({
          name: config.name,
          description: config.description || '',
          type: config.type || 'llm',  // 🆕 加载类型
          scenario: config.scenario,
          provider: config.provider || detectedProvider,
          api_key: config.api_key,
          base_url: config.base_url,
          model: config.model,
          temperature: config.temperature,
          max_tokens: config.max_tokens,
          top_p: config.top_p,
          frequency_penalty: config.frequency_penalty,
          presence_penalty: config.presence_penalty,
          timeout: config.timeout,
          max_retries: config.max_retries,
          is_active: config.is_active,
          priority: config.priority,
          dimensions: config.extra_data?.dimensions,  // 🆕 从 extra_data 提取
        })
        setAdvancedOpen(config.top_p !== 1.0 || config.frequency_penalty !== 0 || config.presence_penalty !== 0)
      } else {
        // 创建模式：使用默认值（302.AI）
        setFormData({
          name: '',
          description: '',
          type: 'llm',  // 🆕 创建时默认LLM
          scenario: 'general',
          provider: '302ai',
          api_key: '',
          base_url: 'https://api.302.ai',
          model: 'sophnet/Qwen3-30B-A3B-Thinking-2507',
          temperature: 0.7,
          max_tokens: 8000,
          top_p: 1.0,
          frequency_penalty: 0.0,
          presence_penalty: 0.0,
          timeout: 600,
          max_retries: 3,
          is_active: true,
          priority: 0,
          dimensions: undefined,
        })
        setAdvancedOpen(false)
      }
      setErrors({})
    }
  }, [open, config])

  // 供应商变更时，自动填充预设值
  const handleProviderChange = (provider: string) => {
    const preset = PROVIDER_PRESETS[provider]
    if (preset && preset.base_url) {
      setFormData({
        ...formData,
        provider,
        base_url: preset.base_url,
        model: preset.model,
      })
    } else {
      // 自定义模式：清空预设值
      setFormData({
        ...formData,
        provider,
        base_url: '',
        model: '',
      })
    }
  }

  const validateForm = (): boolean => {
    const newErrors: Partial<Record<keyof ModelConfigFormData, string>> = {}

    if (!formData.name.trim()) newErrors.name = '请输入配置名称'
    if (!formData.api_key.trim()) newErrors.api_key = '请输入 API Key'
    if (!formData.base_url.trim()) newErrors.base_url = '请输入 Base URL'
    if (!formData.model.trim()) newErrors.model = '请输入模型名称'
    
    // LLM 专用验证
    if (formData.type === 'llm') {
      if (formData.temperature < 0 || formData.temperature > 2) newErrors.temperature = '温度范围: 0-2'
      if (formData.max_tokens < 1) newErrors.max_tokens = '最大 Token 数必须大于 0'
    }

    setErrors(newErrors)
    return Object.keys(newErrors).length === 0
  }

  const handleSubmit = () => {
    if (!validateForm()) return

    // 处理提交数据
    const submitData: any = { ...formData }
    
    // Embedding 类型：将 dimensions 放入 extra_data
    if (formData.type === 'embedding') {
      if (formData.dimensions) {
        submitData.extra_data = { dimensions: formData.dimensions }
      }
      delete submitData.dimensions
    } else {
      // LLM 类型：删除 dimensions 字段
      delete submitData.dimensions
    }

    onSubmit(submitData)
  }

  if (!open) return null

  return (
    <AnimatePresence>
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 pb-24">
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="absolute inset-0 bg-black/20 backdrop-blur-md"
          onClick={onClose}
        />

        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 20 }}
          transition={{ type: 'spring', stiffness: 380, damping: 30 }}
          className="relative w-full max-w-2xl max-h-[calc(100vh-120px)] bg-white rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        >
          {/* 头部 */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
            <h2 className="text-lg font-semibold text-gray-800">
              {isEditMode ? '编辑模型配置' : '创建模型配置'}
            </h2>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors"
            >
              <X className="w-5 h-5 text-gray-500" />
            </button>
          </div>

          {/* 内容 */}
          <div className="flex-1 overflow-y-auto p-6" style={{ minHeight: 0 }}>
            <div className="space-y-6">
              {/* 基本信息 */}
              <div className="space-y-4">
                <div className="space-y-4">
                  <div>
                    <Label htmlFor="name">配置名称 *</Label>
                    <Input
                      id="name"
                      value={formData.name}
                      onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                      placeholder="配置名称"
                      className={errors.name ? 'border-red-500' : ''}
                    />
                    {errors.name && <p className="text-xs text-red-500 mt-1">{errors.name}</p>}
                  </div>

                  <div>
                    <Label htmlFor="provider">供应商 *</Label>
                    <Select
                      value={formData.provider}
                      onValueChange={handleProviderChange}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="302ai">302.AI</SelectItem>
                        <SelectItem value="custom">自定义</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label htmlFor="type">模型类型 *</Label>
                    <Select
                      value={formData.type}
                      onValueChange={(value: any) => {
                        setFormData({ 
                          ...formData, 
                          type: value,
                          scenario: value === 'embedding' ? 'general' : formData.scenario,
                          model: value === 'embedding' ? 'Qwen/Qwen3-Embedding-0.6B' : formData.model
                        })
                      }}
                      disabled={isEditMode}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="llm">LLM 模型</SelectItem>
                        <SelectItem value="embedding">Embedding 模型</SelectItem>
                      </SelectContent>
                    </Select>
                    {isEditMode && <p className="text-xs text-gray-500 mt-1">编辑时不可修改类型</p>}
                  </div>

                  <div>
                    <Label htmlFor="scenario">使用场景 *</Label>
                    <Select
                      value={formData.scenario}
                      onValueChange={(value: any) => setFormData({ ...formData, scenario: value })}
                      disabled={formData.type === 'embedding'}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="general">通用场景</SelectItem>
                        <SelectItem value="extract">数据提取</SelectItem>
                        <SelectItem value="search">智能搜索</SelectItem>
                        <SelectItem value="chat">对话交互</SelectItem>
                        <SelectItem value="summary">内容摘要</SelectItem>
                      </SelectContent>
                    </Select>
                    {formData.type === 'embedding' && (
                      <p className="text-xs text-gray-500 mt-1">Embedding 暂只支持通用场景</p>
                    )}
                  </div>
                </div>

                <div>
                    <Label htmlFor="description">配置说明</Label>
                    <Textarea
                      id="description"
                      value={formData.description}
                      onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                      placeholder="描述此配置的用途..."
                      rows={2}
                    />
                </div>
              </div>

              {/* API 配置 */}
              <div className="space-y-4 pt-4 border-t">
                <h3 className="text-sm font-semibold text-gray-700">API 配置</h3>
                <div className="space-y-4">
                  <div>
                    <Label htmlFor="model">模型名称 *</Label>
                    <Input
                      id="model"
                      value={formData.model}
                      onChange={(e) => setFormData({ ...formData, model: e.target.value })}
                      placeholder="sophnet/Qwen3-30B-A3B-Thinking-2507"
                      className={errors.model ? 'border-red-500' : ''}
                    />
                    {errors.model && <p className="text-xs text-red-500 mt-1">{errors.model}</p>}
                  </div>

                  <div>
                    <Label htmlFor="base_url">Base URL *</Label>
                    <Input
                      id="base_url"
                      value={formData.base_url}
                      onChange={(e) => setFormData({ ...formData, base_url: e.target.value })}
                      placeholder="https://api.302.ai"
                      className={errors.base_url ? 'border-red-500' : ''}
                    />
                    {errors.base_url && <p className="text-xs text-red-500 mt-1">{errors.base_url}</p>}
                  </div>

                  <div>
                    <Label htmlFor="api_key">API Key *</Label>
                    <Input
                      id="api_key"
                      type="password"
                      value={formData.api_key}
                      onChange={(e) => setFormData({ ...formData, api_key: e.target.value })}
                      placeholder="sk-..."
                      className={errors.api_key ? 'border-red-500' : ''}
                    />
                    <div className="flex items-center gap-2 mt-1">
                      {errors.api_key && <p className="text-xs text-red-500">{errors.api_key}</p>}
                      {!errors.api_key && (
                        <a
                          href="https://302.ai/"
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-xs text-purple-600 hover:text-purple-700 hover:underline"
                        >
                          获取 API Key →
                        </a>
                      )}
                    </div>
                  </div>
                </div>
              </div>

              {/* LLM 专用参数 */}
              {formData.type === 'llm' && (
                <>
                  <div className="space-y-4 pt-4 border-t">
                    <h3 className="text-sm font-semibold text-gray-700">LLM 参数</h3>
                    <div className="grid grid-cols-3 gap-4">
                      <div>
                        <Label htmlFor="temperature">温度 (0-2)</Label>
                        <Input
                          id="temperature"
                          type="number"
                          step="0.1"
                          min="0"
                          max="2"
                          value={formData.temperature}
                          onChange={(e) => setFormData({ ...formData, temperature: parseFloat(e.target.value) || 0 })}
                          className={errors.temperature ? 'border-red-500' : ''}
                        />
                      </div>

                      <div>
                        <Label htmlFor="max_tokens">最大 Token</Label>
                        <Input
                          id="max_tokens"
                          type="number"
                          min="1"
                          value={formData.max_tokens}
                          onChange={(e) => setFormData({ ...formData, max_tokens: parseInt(e.target.value) || 1 })}
                        />
                      </div>

                      <div>
                        <Label htmlFor="timeout">超时(秒)</Label>
                        <Input
                          id="timeout"
                          type="number"
                          min="1"
                          value={formData.timeout}
                          onChange={(e) => setFormData({ ...formData, timeout: parseInt(e.target.value) || 1 })}
                        />
                      </div>
                    </div>
                  </div>

                  {/* 高级参数 - 折叠面板 */}
                  <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen} className="pt-4 border-t">
                <CollapsibleTrigger className="flex items-center justify-between w-full text-sm font-semibold text-gray-700 hover:text-gray-900">
                  <span>高级参数（可选）</span>
                  <ChevronDown className={`w-4 h-4 transition-transform ${advancedOpen ? 'rotate-180' : ''}`} />
                </CollapsibleTrigger>
                <CollapsibleContent className="pt-4">
                  <div className="grid grid-cols-3 gap-4">
                    <div>
                      <Label htmlFor="top_p">Top P</Label>
                      <Input
                        id="top_p"
                        type="number"
                        step="0.1"
                        min="0"
                        max="1"
                        value={formData.top_p}
                        onChange={(e) => setFormData({ ...formData, top_p: parseFloat(e.target.value) || 0 })}
                      />
                    </div>

                    <div>
                      <Label htmlFor="frequency_penalty">频率惩罚</Label>
                      <Input
                        id="frequency_penalty"
                        type="number"
                        step="0.1"
                        min="-2"
                        max="2"
                        value={formData.frequency_penalty}
                        onChange={(e) => setFormData({ ...formData, frequency_penalty: parseFloat(e.target.value) || 0 })}
                      />
                    </div>

                    <div>
                      <Label htmlFor="presence_penalty">存在惩罚</Label>
                      <Input
                        id="presence_penalty"
                        type="number"
                        step="0.1"
                        min="-2"
                        max="2"
                        value={formData.presence_penalty}
                        onChange={(e) => setFormData({ ...formData, presence_penalty: parseFloat(e.target.value) || 0 })}
                      />
                    </div>

                    <div>
                      <Label htmlFor="max_retries">最大重试</Label>
                      <Input
                        id="max_retries"
                        type="number"
                        min="0"
                        value={formData.max_retries}
                        onChange={(e) => setFormData({ ...formData, max_retries: parseInt(e.target.value) || 0 })}
                      />
                    </div>

                    <div>
                      <Label htmlFor="priority">优先级</Label>
                      <Input
                        id="priority"
                        type="number"
                        value={formData.priority}
                        onChange={(e) => setFormData({ ...formData, priority: parseInt(e.target.value) || 0 })}
                        placeholder="数字越大优先级越高"
                      />
                    </div>

                    <div className="flex items-end">
                      <div className="flex items-center justify-between w-full pb-2">
                        <Label htmlFor="is_active" className="cursor-pointer">启用配置</Label>
                        <Switch
                          id="is_active"
                          checked={formData.is_active}
                          onCheckedChange={(checked) => setFormData({ ...formData, is_active: checked })}
                        />
                      </div>
                    </div>
                  </div>
                </CollapsibleContent>
              </Collapsible>
                </>
              )}

              {/* Embedding 专用参数 */}
              {formData.type === 'embedding' && (
                <div className="space-y-4 pt-4 border-t">
                  <h3 className="text-sm font-semibold text-gray-700">Embedding 参数</h3>
                  <div className="space-y-4">
                    <div>
                      <Label htmlFor="dimensions">向量维度（可选）</Label>
                      <Input
                        id="dimensions"
                        type="number"
                        placeholder="留空使用模型默认维度"
                        value={formData.dimensions || ''}
                        onChange={(e) => setFormData({
                          ...formData,
                          dimensions: e.target.value ? parseInt(e.target.value) : undefined
                        })}
                      />
                      <p className="text-xs text-gray-500 mt-1">
                        Qwen/Qwen3-Embedding-0.6B 默认1536，text-embedding-3-large 默认3072
                      </p>
                    </div>
                    
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <Label htmlFor="emb_timeout">超时(秒)</Label>
                        <Input
                          id="emb_timeout"
                          type="number"
                          min="1"
                          value={formData.timeout}
                          onChange={(e) => setFormData({ ...formData, timeout: parseInt(e.target.value) || 60 })}
                        />
                      </div>
                      <div>
                        <Label htmlFor="emb_retries">最大重试</Label>
                        <Input
                          id="emb_retries"
                          type="number"
                          min="0"
                          value={formData.max_retries}
                          onChange={(e) => setFormData({ ...formData, max_retries: parseInt(e.target.value) || 3 })}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* 底部按钮 */}
          <div className="flex-shrink-0 flex gap-3 px-6 py-4 bg-gray-50 border-t shadow-[0_-2px_10px_rgba(0,0,0,0.05)]">
            <motion.button
              type="button"
              onClick={onClose}
              disabled={isLoading}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
              className="flex-1 px-4 py-2.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              取消
            </motion.button>
            <motion.button
              type="button"
              onClick={handleSubmit}
              disabled={isLoading}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
              className="flex-1 px-4 py-2.5 text-sm font-medium text-white bg-purple-500 rounded-lg hover:bg-purple-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2"
            >
              {isLoading && <Loader2 className="w-4 h-4 animate-spin" />}
              {isEditMode ? '保存' : '创建'}
            </motion.button>
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  )
}