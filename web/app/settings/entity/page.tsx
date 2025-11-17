'use client'

import { useState, useMemo, useEffect, Suspense } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'next/navigation'
import Link from 'next/link'
import { Settings as SettingsIcon, Plus, ChevronRight, Box } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { apiClient } from '@/lib/api-client'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card'
import { EntityType } from '@/types'
import { EntityTypeCard } from '@/components/settings/EntityTypeCard'
import { EntityTypeDialog, EntityTypeFormData } from '@/components/settings/EntityTypeDialog'
import { DeleteConfirmDialog } from '@/components/settings/DeleteConfirmDialog'
import { AttributeFilter, FilterType } from '@/components/settings/AttributeFilter'
import { toast } from 'sonner'

// 系统默认类型的排序顺序：时间-地点-人物-行为-话题-标签
const DEFAULT_TYPE_ORDER = ['time', 'location', 'person', 'action', 'topic', 'tags']

// 对系统默认类型进行排序的函数
const sortDefaultTypes = (types: any[]) => {
  return [...types].sort((a, b) => {
    const indexA = DEFAULT_TYPE_ORDER.indexOf(a.type)
    const indexB = DEFAULT_TYPE_ORDER.indexOf(b.type)
    // 如果类型不在排序列表中，放在最后
    if (indexA === -1 && indexB === -1) return 0
    if (indexA === -1) return 1
    if (indexB === -1) return -1
    return indexA - indexB
  })
}

function SettingsContent() {
  const queryClient = useQueryClient()
  const searchParams = useSearchParams()

  // 从 URL 读取 source_id 参数
  const urlSourceId = searchParams.get('source_id')

  // 筛选器状态（支持动态 source_id）
  const [filterType, setFilterType] = useState<FilterType>('all')

  // 根据 URL 参数设置初始筛选
  useEffect(() => {
    if (urlSourceId) {
      setFilterType(urlSourceId)
    }
  }, [urlSourceId])

  // 对话框状态
  const [isCreateDialogOpen, setIsCreateDialogOpen] = useState(false)
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false)
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false)
  const [selectedEntityType, setSelectedEntityType] = useState<EntityType | null>(null)

  // 获取系统默认类型
  const { data: defaultTypesData, isLoading: isLoadingDefaults } = useQuery({
    queryKey: ['defaultEntityTypes'],
    queryFn: () => apiClient.getDefaultEntityTypes(),
  })

  // 获取全局自定义类型
  const { data: globalTypesData, isLoading: isLoadingGlobal } = useQuery({
    queryKey: ['globalEntityTypes'],
    queryFn: () => apiClient.getGlobalEntityTypes({ page: 1, page_size: 100, only_active: false }),
  })

  // 获取所有信息源
  const { data: sourcesData } = useQuery({
    queryKey: ['sources'],
    queryFn: () => apiClient.getSources({ page: 1, page_size: 100 }),
  })

  const sourcesList = sourcesData?.data || []

  // 获取所有文档
  const { data: articlesData } = useQuery({
    queryKey: ['articles'],
    queryFn: () => apiClient.getArticles({ page: 1, page_size: 1000 }),
  })

  const articlesList = articlesData?.data || []

  // 懒加载：仅当选择特定信息源时才加载其专属属性
  const isSourceSelected = useMemo(() => {
    return sourcesList.some((s: any) => s.id === filterType)
  }, [filterType, sourcesList])

  const selectedSourceId = isSourceSelected ? filterType : null
  const selectedSourceName = useMemo(() => {
    if (!selectedSourceId) return null
    return sourcesList.find((s: any) => s.id === selectedSourceId)?.name
  }, [selectedSourceId, sourcesList])

  const { data: selectedSourceAttributesData, isLoading: isLoadingSourceAttributes } = useQuery({
    queryKey: ['sourceAttributes', selectedSourceId],
    queryFn: () => apiClient.getEntityTypes(selectedSourceId!, {
      include_defaults: false,
      page_size: 100,
      only_active: false
    }),
    enabled: !!selectedSourceId,
  })

  // 新增：当 filterType 为 'all' 时，调用聚合接口获取所有实体类型
  const { data: allEntityTypesData, isLoading: isLoadingAllEntityTypes } = useQuery({
    queryKey: ['allEntityTypes'],
    queryFn: () => apiClient.getAllEntityTypes({
      page: 1,
      page_size: 1000,
      only_active: false
    }),
    enabled: filterType === 'all',
  })

  const defaultTypes = defaultTypesData?.data || []
  const globalTypes = globalTypesData?.data || []
  const selectedSourceAttributes = (selectedSourceAttributesData?.data || []).map((attr: EntityType) => ({
    ...attr,
    _sourceName: selectedSourceName,
  }))
  const allEntityTypes = allEntityTypesData?.data || []

  // 合并所有属性并根据筛选器过滤（支持动态 source_id）
  const allAttributes = useMemo(() => {
    switch (filterType) {
      case 'default':
        return sortDefaultTypes(defaultTypes)
      case 'global':
        return globalTypes
      case 'all':
        // "全部"视图：使用聚合接口返回的所有类型（系统默认 + 全局 + 所有源专属）
        return allEntityTypes
      default:
        // 具体的 source_id：显示该信息源的专属属性
        if (isSourceSelected) {
          return selectedSourceAttributes
        }
        return [...sortDefaultTypes(defaultTypes), ...globalTypes]
    }
  }, [filterType, defaultTypes, globalTypes, allEntityTypes, selectedSourceAttributes, isSourceSelected])

  // 计算数量（使用 source 的统计字段 + 懒加载当前选中的详情）

  const counts = useMemo(() => {
    // 计算所有信息源的属性总数
    const sourcesTotal = sourcesList.reduce((sum: number, source: any) => {
      return sum + (source.entity_types_count || 0)
    }, 0)
    
    const result: {
      all: number
      default: number
      global: number
      [key: string]: number
    } = {
      all: defaultTypes.length + globalTypes.length + sourcesTotal,
      default: defaultTypes.length,
      global: globalTypes.length,
    }

    // 为每个信息源显示从后端获取的统计数量
    sourcesList.forEach((source: any) => {
      result[source.id] = source.entity_types_count || 0
    })

    return result
  }, [defaultTypes, globalTypes, sourcesList])

  // 创建分组数据（用于"全部属性"视图）
  const groupedAttributes = useMemo(() => {
    const groups: Array<{ 
      title: string
      count: number
      items: any[]
      type?: 'default' | 'global' | 'source' | 'article'
      isSourceGroup?: boolean
      isArticleGroup?: boolean
      subGroups?: Array<{
        title: string
        count: number
        items: any[]
        isArticleGroup: boolean
      }>
    }> = []

    if (filterType === 'all' && allEntityTypes.length > 0) {
      // 1. 系统默认分组
      const defaultItems = sortDefaultTypes(allEntityTypes.filter((attr: any) => attr.is_default))
      if (defaultItems.length > 0) {
        groups.push({
          title: '系统默认',
          count: defaultItems.length,
          items: defaultItems,
          type: 'default',
        })
      }

      // 2. 全局通用分组
      const globalItems = allEntityTypes.filter(
        (attr: any) => !attr.is_default && attr.scope === 'global'
      )
      if (globalItems.length > 0) {
        groups.push({
          title: '通用',
          count: globalItems.length,
          items: globalItems,
          type: 'global',
        })
      }

      // 3. 按信息源分组（包含该信息源的属性 + 该信息源下所有文档的属性）
      const sourceGroupsMap = new Map<string, {
        sourceName: string
        sourceItems: any[]
        articleGroups: Map<string, { title: string; items: any[] }>
      }>()

      // 收集信息源级别的属性
      allEntityTypes.forEach((attr: any) => {
        if (attr.scope === 'source' && attr.source_id && !attr.article_id) {
          if (!sourceGroupsMap.has(attr.source_id)) {
            sourceGroupsMap.set(attr.source_id, {
              sourceName: attr._sourceName || sourcesList.find((s: any) => s.id === attr.source_id)?.name || '未知信息源',
              sourceItems: [],
              articleGroups: new Map(),
            })
          }
          sourceGroupsMap.get(attr.source_id)!.sourceItems.push(attr)
        }
      })

      // 收集文档级别的属性（归类到对应的信息源）
      allEntityTypes.forEach((attr: any) => {
        if (attr.scope === 'article' && attr.article_id && attr.source_id) {
          if (!sourceGroupsMap.has(attr.source_id)) {
            sourceGroupsMap.set(attr.source_id, {
              sourceName: attr._sourceName || sourcesList.find((s: any) => s.id === attr.source_id)?.name || '未知信息源',
              sourceItems: [],
              articleGroups: new Map(),
            })
          }
          
          const sourceGroup = sourceGroupsMap.get(attr.source_id)!
          if (!sourceGroup.articleGroups.has(attr.article_id)) {
            sourceGroup.articleGroups.set(attr.article_id, {
              title: attr._articleTitle || articlesList.find((a: any) => a.id === attr.article_id)?.title || '未知文档',
              items: [],
            })
          }
          sourceGroup.articleGroups.get(attr.article_id)!.items.push(attr)
        }
      })

      // 构建最终的分组结构
      sourceGroupsMap.forEach((sourceData) => {
        const subGroups: Array<{
          title: string
          count: number
          items: any[]
          isArticleGroup: boolean
        }> = []

        // 添加文档子分组
        sourceData.articleGroups.forEach((articleData) => {
          subGroups.push({
            title: articleData.title,
            count: articleData.items.length,
            items: articleData.items,
            isArticleGroup: true,
          })
        })

        groups.push({
          title: sourceData.sourceName,
          count: sourceData.sourceItems.length,
          items: sourceData.sourceItems,
          type: 'source',
          isSourceGroup: true,
          subGroups: subGroups.length > 0 ? subGroups : undefined,
        })
      })
    } else {
      // 其他视图保持原有逻辑（只显示默认和通用）
      if (defaultTypes.length > 0) {
        groups.push({
          title: '系统默认',
          count: defaultTypes.length,
          items: sortDefaultTypes(defaultTypes),
          type: 'default',
        })
      }

      if (globalTypes.length > 0) {
        groups.push({
          title: '通用',
          count: globalTypes.length,
          items: globalTypes,
          type: 'global',
        })
      }
    }

    return groups
  }, [filterType, allEntityTypes, defaultTypes, globalTypes, sourcesList, articlesList])

  // 创建 Mutation - 完善三种范围的处理
  const createMutation = useMutation({
    mutationFn: (data: EntityTypeFormData) => {
      // 根据 scope 选择不同的 API
      if (data.scope === 'global') {
        // 全局属性：不绑定信息源和文档
        return apiClient.createGlobalEntityType(data)
      } else if (data.scope === 'article' && data.article_id) {
        // 文档级别属性：绑定到特定文档
        // 后端会自动从文档中获取 source_id
        return apiClient.createArticleEntityType(data.article_id, data)
      } else if (data.scope === 'source' && data.source_config_id) {
        // 信息源级别属性：绑定到特定信息源
        return apiClient.createEntityType(data.source_config_id, data)
      } else {
        // 数据验证失败
        return Promise.reject(new Error('无效的范围配置：请选择有效的应用范围'))
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['globalEntityTypes'] })
      queryClient.invalidateQueries({ queryKey: ['sourceAttributes'] })
      queryClient.invalidateQueries({ queryKey: ['allEntityTypes'] })
      queryClient.invalidateQueries({ queryKey: ['sources'] })  // 刷新信息源列表（更新统计数量）
      setIsCreateDialogOpen(false)
      toast.success('数据属性创建成功')
    },
    onError: (error: any) => {
      const errorMsg = error.response?.data?.error?.message || error.message || '创建失败'
      toast.error(errorMsg)
    },
  })

  // 更新 Mutation
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<EntityTypeFormData> }) =>
      apiClient.updateEntityType(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['globalEntityTypes'] })
      queryClient.invalidateQueries({ queryKey: ['sourceAttributes'] })
      queryClient.invalidateQueries({ queryKey: ['allEntityTypes'] })
      setIsEditDialogOpen(false)
      setSelectedEntityType(null)
      toast.success('数据属性更新成功')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error?.message || '更新失败')
    },
  })

  // 删除 Mutation
  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiClient.deleteEntityType(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['globalEntityTypes'] })
      queryClient.invalidateQueries({ queryKey: ['sourceAttributes'] })
      queryClient.invalidateQueries({ queryKey: ['allEntityTypes'] })
      setIsDeleteDialogOpen(false)
      setSelectedEntityType(null)
      toast.success('数据属性删除成功')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error?.message || '删除失败')
    },
  })

  // 切换 Mutation
  const toggleMutation = useMutation({
    mutationFn: ({ id, isActive }: { id: string; isActive: boolean }) =>
      apiClient.updateEntityType(id, { is_active: isActive }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['globalEntityTypes'] })
      queryClient.invalidateQueries({ queryKey: ['sourceAttributes'] })
      queryClient.invalidateQueries({ queryKey: ['allEntityTypes'] })
      toast.success('属性状态已更新')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error?.message || '更新失败')
    },
  })

  // 处理函数
  const handleCreate = (data: EntityTypeFormData) => {
    createMutation.mutate(data)
  }

  const handleEdit = (entityType: EntityType) => {
    setSelectedEntityType(entityType)
    setIsEditDialogOpen(true)
  }

  const handleUpdate = (data: EntityTypeFormData) => {
    if (selectedEntityType) {
      updateMutation.mutate({ id: selectedEntityType.id, data })
    }
  }

  const handleDelete = (entityType: EntityType) => {
    setSelectedEntityType(entityType)
    setIsDeleteDialogOpen(true)
  }

  const handleConfirmDelete = () => {
    if (selectedEntityType) {
      deleteMutation.mutate(selectedEntityType.id)
    }
  }

  const handleToggle = (entityType: EntityType, isActive: boolean) => {
    toggleMutation.mutate({ id: entityType.id, isActive })
  }

  // 获取信息源名称
  const getSourceName = (sourceId?: string) => {
    if (!sourceId) return undefined
    const source = sourcesList.find((s: any) => s.id === sourceId)
    return source?.name
  }

  const isLoading = isLoadingDefaults || isLoadingGlobal || (filterType === 'all' && isLoadingAllEntityTypes) || (isSourceSelected && isLoadingSourceAttributes)

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
          <Box className="w-4 h-4" />
          <span>实体维度</span>
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
          <div className="p-2.5 rounded-xl bg-gradient-to-br from-amber-400 to-yellow-500 shadow-md">
            <Box className="w-6 h-6 text-white" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-800">实体维度设置</h1>
            <p className="text-gray-500 text-xs">管理数据提取的属性类型配置</p>
          </div>
        </div>
        <button
          onClick={() => setIsCreateDialogOpen(true)}
          className="h-11 w-11 p-0 rounded-full bg-gray-100 text-gray-600 hover:bg-gray-200 shadow-lg hover:shadow-xl transition-all flex items-center justify-center"
        >
          <Plus className="w-5 h-5" />
        </button>
      </motion.div>

      {/* 筛选器 */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.1 }}
      >
        <AttributeFilter
          value={filterType}
          onChange={setFilterType}
          sources={sourcesList.map((s: any) => ({ id: s.id, name: s.name }))}
          counts={counts}
        />
      </motion.div>

      {/* 属性列表 */}
      {isLoading ? (
        <div className="text-center py-12">
          <div className="w-10 h-10 mx-auto mb-3 rounded-lg bg-gray-100 flex items-center justify-center animate-pulse">
            <SettingsIcon className="w-5 h-5 text-gray-400 animate-spin" />
          </div>
          <p className="text-sm text-gray-500">加载中...</p>
        </div>
      ) : allAttributes.length === 0 ? (
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
            {filterType === 'all' ? '暂无数据属性' : '暂无符合条件的属性'}
          </p>
          <p className="text-xs text-gray-500">
            点击右上角"+"按钮添加新的数据属性
          </p>
        </motion.div>
      ) : filterType === 'all' ? (
        // 分组展示（全部属性视图）
        <div className="space-y-8">
          {groupedAttributes.map((group, groupIndex) => {
            // 判断是否需要在信息源分组前显示大分隔符
            const showSourceDivider = group.isSourceGroup && groupIndex > 0 && 
              !groupedAttributes[groupIndex - 1].isSourceGroup

            return (
              <div key={`${group.type}-${group.title}`}>
                {/* 信息源大分隔符（在每个信息源前显示） */}
                {group.isSourceGroup && (
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
                  {/* 主分组标题（信息源或系统默认/通用） */}
              <div className="flex items-baseline gap-2">
                    <h2 className="text-sm font-semibold text-gray-700">
                      {group.title}
                    </h2>
                <span className="text-xs text-gray-400">({group.count})</span>
              </div>

                  {/* 主分组卡片网格 */}
                  {group.items.length > 0 && (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {group.items.map((attr: any, index: number) => (
                  <EntityTypeCard
                    key={attr.id}
                    entityType={attr}
                    sourceName={attr._sourceName || getSourceName(attr.source_id)}
                          articleTitle={attr._articleTitle}
                          onEdit={handleEdit}
                          onDelete={handleDelete}
                          onToggle={handleToggle}
                          index={index}
                        />
                      ))}
                    </div>
                  )}

                  {/* 文档子分组（紧跟在信息源下） */}
                  {group.subGroups && group.subGroups.map((subGroup, subIndex) => (
                    <div key={`sub-${subIndex}`} className="space-y-4">
                      {/* 文档标题 - 浅色，与信息源标题对齐 */}
                      <div className="flex items-baseline gap-2">
                        <h3 className="text-sm font-semibold text-gray-500">
                          {subGroup.title}
                        </h3>
                        <span className="text-xs text-gray-400">({subGroup.count})</span>
                      </div>

                      {/* 文档卡片网格 */}
                      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                        {subGroup.items.map((attr: any, index: number) => (
                          <EntityTypeCard
                            key={attr.id}
                            entityType={attr}
                            sourceName={attr._sourceName || getSourceName(attr.source_id)}
                            articleTitle={attr._articleTitle}
                    onEdit={handleEdit}
                    onDelete={handleDelete}
                    onToggle={handleToggle}
                    index={index}
                  />
                ))}
              </div>
                    </div>
                  ))}
            </motion.div>
              </div>
            )
          })}
        </div>
      ) : (
        // 平铺展示（筛选视图）
        <motion.div
          className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.5, delay: 0.2 }}
        >
          {allAttributes.map((attr: any, index: number) => (
            <EntityTypeCard
              key={attr.id}
              entityType={attr}
              sourceName={attr._sourceName || getSourceName(attr.source_id)}
              onEdit={handleEdit}
              onDelete={handleDelete}
              onToggle={handleToggle}
              index={index}
            />
          ))}
        </motion.div>
      )}

      {/* 对话框 */}
      <EntityTypeDialog
        open={isCreateDialogOpen}
        onClose={() => setIsCreateDialogOpen(false)}
        onSubmit={handleCreate}
        sources={sourcesList.map((s: any) => ({ id: s.id, name: s.name }))}
        articles={articlesList.map((a: any) => ({ id: a.id, title: a.title, source_id: a.source_id }))}
        isLoading={createMutation.isPending}
      />

      <EntityTypeDialog
        open={isEditDialogOpen}
        onClose={() => {
          setIsEditDialogOpen(false)
          setSelectedEntityType(null)
        }}
        onSubmit={handleUpdate}
        entityType={selectedEntityType}
        sources={sourcesList.map((s: any) => ({ id: s.id, name: s.name }))}
        articles={articlesList.map((a: any) => ({ id: a.id, title: a.title, source_id: a.source_id }))}
        isLoading={updateMutation.isPending}
      />

      <DeleteConfirmDialog
        open={isDeleteDialogOpen}
        onClose={() => {
          setIsDeleteDialogOpen(false)
          setSelectedEntityType(null)
        }}
        onConfirm={handleConfirmDelete}
        entityType={selectedEntityType}
        isLoading={deleteMutation.isPending}
      />
    </div>
  )
}

// Loading fallback component
function SettingsLoadingFallback() {
  return (
    <div className="space-y-8 pb-8">
      <div className="flex justify-between items-center">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-gray-100 shadow-md">
            <SettingsIcon className="w-6 h-6 text-gray-600" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-800">属性设置</h1>
            <p className="text-gray-500 text-xs">管理数据属性配置</p>
          </div>
        </div>
      </div>
      <div className="text-center py-12">
        <div className="w-10 h-10 mx-auto mb-3 rounded-lg bg-gray-100 flex items-center justify-center animate-pulse">
          <SettingsIcon className="w-5 h-5 text-gray-400 animate-spin" />
        </div>
        <p className="text-sm text-gray-500">加载中...</p>
      </div>
    </div>
  )
}

// Main page component with Suspense boundary
export default function SettingsPage() {
  return (
    <Suspense fallback={<SettingsLoadingFallback />}>
      <SettingsContent />
    </Suspense>
  )
}
