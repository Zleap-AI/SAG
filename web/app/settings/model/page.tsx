'use client'

import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import { Settings as SettingsIcon, Plus, ChevronRight, Sparkles } from 'lucide-react'
import { motion } from 'framer-motion'
import { apiClient } from '@/lib/api-client'
import { ModelConfig } from '@/types'
import { ModelConfigFilter, ModelFilterType } from '@/components/settings/ModelConfigFilter'
import { ModelConfigCard } from '@/components/settings/ModelConfigCard'
import { ModelConfigDialog, ModelConfigFormData } from '@/components/settings/ModelConfigDialog'
import { ModelConfigDeleteDialog } from '@/components/settings/ModelConfigDeleteDialog'
import { toast } from 'sonner'

export default function ModelSettingsPage() {
  const queryClient = useQueryClient()

  const [filterType, setFilterType] = useState<ModelFilterType>('all')
  const [isCreateDialogOpen, setIsCreateDialogOpen] = useState(false)
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false)
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false)
  const [selectedConfig, setSelectedConfig] = useState<ModelConfig | null>(null)

  const { data: configsData, isLoading } = useQuery({
    queryKey: ['modelConfigs'],
    queryFn: () => apiClient.getModelConfigs({}),
  })

  const allConfigs = configsData?.data || []

  const filteredConfigs = useMemo(() => {
    if (filterType === 'all') return allConfigs
    
    // 按类型筛选 'llm' 或 'embedding'
    if (filterType === 'llm' || filterType === 'embedding') {
      return allConfigs.filter((config: ModelConfig) => config.type === filterType)
    }
    
    // 按类型+场景筛选 'llm:general'
    if (filterType.includes(':')) {
      const [type, scenario] = filterType.split(':')
      return allConfigs.filter(
        (config: ModelConfig) => config.type === type && config.scenario === scenario
      )
    }
    
    return allConfigs
  }, [allConfigs, filterType])

  // 场景名称映射
  const scenarioNames: Record<string, string> = {
    extract: '数据提取',
    search: '智能搜索',
    chat: '对话交互',
    summary: '内容摘要',
    general: '通用场景',
  }

  // 创建分组数据（用于分组展示视图）
  const groupedConfigs = useMemo(() => {
    const groups: Array<{
      type: string
      title: string
      count: number
      items: ModelConfig[]
      isTypeGroup?: boolean
      subGroups?: Array<{
        title: string
        count: number
        items: ModelConfig[]
      }>
    }> = []

    // 全部：按 LLM 和 Embedding 分大类，再按场景分组
    if (filterType === 'all') {
      const modelConfigs = allConfigs.filter((c: ModelConfig) => c.type === 'llm')
      const embConfigs = allConfigs.filter((c: ModelConfig) => c.type === 'embedding')
      
      if (modelConfigs.length > 0) {
        // LLM 按场景分组作为子分组
        const llmScenarios = ['general', 'extract', 'search', 'chat', 'summary']
        const subGroups: Array<{
          title: string
          count: number
          items: ModelConfig[]
        }> = []
        
        llmScenarios.forEach((scenario) => {
          const items = modelConfigs.filter((c: ModelConfig) => c.scenario === scenario)
          if (items.length > 0) {
            subGroups.push({
              title: scenarioNames[scenario],
              count: items.length,
              items,
            })
          }
        })
        
        groups.push({
          type: 'llm',
          title: 'LLM 模型',
          count: modelConfigs.length,
          items: [],
          isTypeGroup: true,
          subGroups,
        })
      }
      
      if (embConfigs.length > 0) {
        // Embedding 按场景分组作为子分组
        const subGroups: Array<{
          title: string
          count: number
          items: ModelConfig[]
        }> = [{
          title: '通用场景',
          count: embConfigs.length,
          items: embConfigs,
        }]
        
        groups.push({
          type: 'embedding',
          title: 'Embedding 模型',
          count: embConfigs.length,
          items: [],
          isTypeGroup: true,
          subGroups,
        })
      }
    }
    // LLM 类型：按场景分组
    else if (filterType === 'llm') {
      const llmScenarios = ['general', 'extract', 'search', 'chat', 'summary']
      llmScenarios.forEach((scenario) => {
        const items = allConfigs.filter(
          (c: ModelConfig) => c.type === 'llm' && c.scenario === scenario
        )
        if (items.length > 0) {
          groups.push({
            type: 'llm',
            title: scenarioNames[scenario],
            count: items.length,
            items,
          })
        }
      })
    }
    // Embedding 类型：按场景分组（目前只有通用）
    else if (filterType === 'embedding') {
      const items = allConfigs.filter((c: ModelConfig) => c.type === 'embedding')
      if (items.length > 0) {
        groups.push({
          type: 'embedding',
          title: '通用场景',
          count: items.length,
          items,
        })
      }
    }

    return groups
  }, [filterType, allConfigs, scenarioNames])

  // 统计数据（用于筛选器显示）
  const counts = useMemo(() => {
    const result = {
      all: allConfigs.length,
      llm: 0,
      'llm:general': 0,
      'llm:extract': 0,
      'llm:search': 0,
      'llm:chat': 0,
      'llm:summary': 0,
      embedding: 0,
      'embedding:general': 0,
    }
    
    allConfigs.forEach((config: ModelConfig) => {
      if (config.type === 'llm') {
        result.llm++
        const key = `llm:${config.scenario}` as keyof typeof result
        if (key in result) result[key]++
      } else if (config.type === 'embedding') {
        result.embedding++
        if (config.scenario === 'general') result['embedding:general']++
      }
    })

    return result
  }, [allConfigs])

  const createMutation = useMutation({
    mutationFn: (data: ModelConfigFormData) => apiClient.createModelConfig(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['modelConfigs'] })
      setIsCreateDialogOpen(false)
      toast.success('模型配置创建成功')
    },
    onError: (error: any) => {
      const errorMsg = error.response?.data?.error?.message || error.message || '创建失败'
      toast.error(errorMsg)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<ModelConfigFormData> }) =>
      apiClient.updateModelConfig(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['modelConfigs'] })
      setIsEditDialogOpen(false)
      setSelectedConfig(null)
      toast.success('模型配置更新成功')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error?.message || '更新失败')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiClient.deleteModelConfig(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['modelConfigs'] })
      setIsDeleteDialogOpen(false)
      setSelectedConfig(null)
      toast.success('模型配置删除成功')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error?.message || '删除失败')
    },
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, isActive }: { id: string; isActive: boolean }) =>
      apiClient.updateModelConfig(id, { is_active: isActive }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['modelConfigs'] })
      toast.success('配置状态已更新')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error?.message || '更新失败')
    },
  })

  const handleCreate = (data: ModelConfigFormData) => {
    // 根据当前筛选器状态自动设置类型和场景
    const submitData = { ...data }
    
    if (filterType.startsWith('llm')) {
      submitData.type = submitData.type || 'llm'
      if (filterType.includes(':')) {
        const [, scenario] = filterType.split(':')
        submitData.scenario = submitData.scenario || scenario
      }
    } else if (filterType.startsWith('embedding')) {
      submitData.type = submitData.type || 'embedding'
      submitData.scenario = submitData.scenario || 'general'
    } else {
      submitData.type = submitData.type || 'llm'
    }
    
    createMutation.mutate(submitData)
  }

  const handleEdit = (config: ModelConfig) => {
    setSelectedConfig(config)
    setIsEditDialogOpen(true)
  }

  const handleUpdate = (data: ModelConfigFormData) => {
    if (selectedConfig) {
      updateMutation.mutate({ id: selectedConfig.id, data })
    }
  }

  const handleDelete = (config: ModelConfig) => {
    setSelectedConfig(config)
    setIsDeleteDialogOpen(true)
  }

  const handleConfirmDelete = () => {
    if (selectedConfig) {
      deleteMutation.mutate(selectedConfig.id)
    }
  }

  const handleToggle = (config: ModelConfig, isActive: boolean) => {
    toggleMutation.mutate({ id: config.id, isActive })
  }

  return (
    <div className="space-y-8 pb-8">
      {/* 面包屑导航 */}
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3 }}
        className="flex items-center gap-2 text-sm"
      >
        <Link 
          href="/settings" 
          className="text-gray-500 hover:text-gray-700 transition-colors flex items-center gap-1"
        >
          <SettingsIcon className="w-4 h-4" />
          <span>设置</span>
        </Link>
        <ChevronRight className="w-4 h-4 text-gray-400" />
        <span className="text-gray-900 font-medium flex items-center gap-1">
          <Sparkles className="w-4 h-4" />
          <span>模型配置</span>
        </span>
      </motion.div>

      {/* 页面标题 */}
      <motion.div
        className="flex justify-between items-center"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, delay: 0.1 }}
      >
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-gradient-to-br from-indigo-400 to-purple-400 shadow-md">
            <Sparkles className="w-6 h-6 text-white" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-800">模型配置管理</h1>
            <p className="text-gray-500 text-xs">管理不同场景下的 LLM 模型配置</p>
          </div>
        </div>
        <button
          onClick={() => setIsCreateDialogOpen(true)}
          className="h-11 w-11 p-0 rounded-full bg-gray-100 text-gray-600 hover:bg-gray-200 shadow-lg hover:shadow-xl transition-all flex items-center justify-center"
        >
          <Plus className="w-5 h-5" />
        </button>
      </motion.div>

      {/* 筛选器 - 统一层级筛选 */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.1 }}
      >
        <ModelConfigFilter
          value={filterType}
          onChange={setFilterType}
            counts={counts}
          />
      </motion.div>

      {/* 配置列表 */}
      {isLoading ? (
        <div className="text-center py-12">
          <div className="w-10 h-10 mx-auto mb-3 rounded-lg bg-gray-100 flex items-center justify-center animate-pulse">
            <SettingsIcon className="w-5 h-5 text-gray-400 animate-spin" />
          </div>
          <p className="text-sm text-gray-500">加载中...</p>
        </div>
      ) : filteredConfigs.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.5 }}
          className="text-center py-12"
        >
          <div className="w-16 h-16 mx-auto mb-4 rounded-xl bg-gray-100 flex items-center justify-center">
            <Plus className="w-8 h-8 text-gray-400" />
          </div>
          <p className="text-sm font-medium text-gray-700 mb-1">
            {filterType === 'all' ? '暂无模型配置' : '暂无符合条件的配置'}
          </p>
          <p className="text-xs text-gray-500">
            点击右上角"+"按钮添加新的模型配置
          </p>
        </motion.div>
      ) : (filterType === 'all' || filterType === 'llm' || filterType === 'embedding') ? (
        // 分组展示（全部、LLM、Embedding 视图）
        <div className="space-y-8">
          {groupedConfigs.map((group, groupIndex) => (
            <div key={`${group.type}-${groupIndex}-${group.title}`}>
              {/* 在不同类型之间添加大分隔线 */}
              {groupIndex > 0 && group.isTypeGroup && (
                <div className="mb-6 pt-6">
                  <div className="border-t border-gray-300" />
                </div>
              )}

              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.4, delay: groupIndex * 0.1 }}
                className="space-y-6"
              >
                {/* 大分类标题（类型级别） */}
                {group.isTypeGroup ? (
                  <>
                    <div className="flex items-baseline gap-2">
                      <h2 className="text-sm font-semibold text-gray-700">
                        {group.title}
                      </h2>
                      <span className="text-xs text-gray-400">({group.count})</span>
                    </div>

                    {/* 子分组（场景级别） */}
                    {group.subGroups && group.subGroups.map((subGroup, subIndex) => (
                      <div key={subIndex} className="space-y-4">
                        {/* 场景标题 - 浅色，与类型标题对齐 */}
                        <div className="flex items-baseline gap-2">
                          <h3 className="text-sm font-semibold text-gray-500">
                            {subGroup.title}
                          </h3>
                          <span className="text-xs text-gray-400">({subGroup.count})</span>
                        </div>

                        {/* 场景卡片网格 */}
                        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                          {subGroup.items.map((config: ModelConfig, index: number) => (
                            <ModelConfigCard
                              key={config.id}
                              config={config}
                              onEdit={handleEdit}
                              onDelete={handleDelete}
                              onToggle={handleToggle}
                              index={index}
                            />
                          ))}
                        </div>
                      </div>
                    ))}
                  </>
                ) : (
                  <>
                    {/* 普通分组标题 */}
                <div className="flex items-baseline gap-2">
                  <h2 className="text-sm font-semibold text-gray-700">
                    {group.title}
                  </h2>
                  <span className="text-xs text-gray-400">({group.count})</span>
                </div>

                {/* 卡片网格 */}
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                  {group.items.map((config: ModelConfig, index: number) => (
                    <ModelConfigCard
                      key={config.id}
                      config={config}
                      onEdit={handleEdit}
                      onDelete={handleDelete}
                      onToggle={handleToggle}
                      index={index}
                    />
                  ))}
                </div>
                  </>
                )}
              </motion.div>
            </div>
          ))}
        </div>
      ) : (
        // 平铺展示（选择具体场景时）
        <motion.div
          className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.5, delay: 0.2 }}
        >
          {filteredConfigs.map((config: ModelConfig, index: number) => (
            <ModelConfigCard
              key={config.id}
              config={config}
              onEdit={handleEdit}
              onDelete={handleDelete}
              onToggle={handleToggle}
              index={index}
            />
          ))}
        </motion.div>
      )}

      {/* 对话框 */}
      <ModelConfigDialog
        open={isCreateDialogOpen}
        onClose={() => setIsCreateDialogOpen(false)}
        onSubmit={handleCreate}
        isLoading={createMutation.isPending}
      />

      <ModelConfigDialog
        open={isEditDialogOpen}
        onClose={() => {
          setIsEditDialogOpen(false)
          setSelectedConfig(null)
        }}
        onSubmit={handleUpdate}
        config={selectedConfig}
        isLoading={updateMutation.isPending}
      />

      <ModelConfigDeleteDialog
        open={isDeleteDialogOpen}
        onClose={() => {
          setIsDeleteDialogOpen(false)
          setSelectedConfig(null)
        }}
        onConfirm={handleConfirmDelete}
        config={selectedConfig}
        isLoading={deleteMutation.isPending}
      />
    </div>
  )
}