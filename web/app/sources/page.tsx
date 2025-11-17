'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { Plus, Database } from 'lucide-react'
import { apiClient } from '@/lib/api-client'
import { Source } from '@/types'
import { SourceCard } from '@/components/sources/SourceCard'
import { SourceDialog, SourceFormData } from '@/components/sources/SourceDialog'
import { DeleteConfirmDialog } from '@/components/settings/DeleteConfirmDialog'
import { toast } from 'sonner'

export default function SourcesPage() {
  const queryClient = useQueryClient()

  // 对话框状态
  const [isCreateDialogOpen, setIsCreateDialogOpen] = useState(false)
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false)
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false)
  const [selectedSource, setSelectedSource] = useState<Source | null>(null)

  // 获取信息源列表
  const { data: sourcesData, isLoading } = useQuery({
    queryKey: ['sources'],
    queryFn: () => apiClient.getSources(),
  })

  const sources = sourcesData?.data || []

  // 创建 Mutation
  const createMutation = useMutation({
    mutationFn: (data: SourceFormData) => apiClient.createSource(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] })
      setIsCreateDialogOpen(false)
      toast.success('信息源创建成功')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error?.message || '创建失败')
    },
  })

  // 更新 Mutation
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: SourceFormData }) =>
      apiClient.updateSource(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] })
      setIsEditDialogOpen(false)
      setSelectedSource(null)
      toast.success('信息源更新成功')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error?.message || '更新失败')
    },
  })

  // 删除 Mutation
  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiClient.deleteSource(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] })
      setIsDeleteDialogOpen(false)
      setSelectedSource(null)
      toast.success('信息源删除成功')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.error?.message || '删除失败')
    },
  })

  // 处理函数
  const handleCreate = (data: SourceFormData) => {
    createMutation.mutate(data)
  }

  const handleEdit = (source: Source) => {
    setSelectedSource(source)
    setIsEditDialogOpen(true)
  }

  const handleUpdate = (data: SourceFormData) => {
    if (selectedSource) {
      updateMutation.mutate({ id: selectedSource.id, data })
    }
  }

  const handleDelete = (source: Source) => {
    setSelectedSource(source)
    setIsDeleteDialogOpen(true)
  }

  const handleConfirmDelete = () => {
    if (selectedSource) {
      deleteMutation.mutate(selectedSource.id)
    }
  }

  return (
    <div className="space-y-8 pb-8">
      {/* 页面标题 */}
      <motion.div
        className="flex justify-between items-center"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-emerald-50 shadow-md">
            <Database className="w-6 h-6 text-emerald-600" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-800">信息源管理</h1>
            <p className="text-gray-500 text-xs">管理不同的数据源，隔离不同来源的数据</p>
          </div>
        </div>
        <button
          onClick={() => setIsCreateDialogOpen(true)}
          className="h-11 w-11 p-0 rounded-full bg-gray-100 text-gray-600 hover:bg-gray-200 shadow-lg hover:shadow-xl transition-all flex items-center justify-center"
        >
          <Plus className="w-5 h-5" />
        </button>
      </motion.div>

      {/* 信息源列表 */}
      {isLoading ? (
        <div className="text-center py-12">
          <div className="w-10 h-10 mx-auto mb-3 rounded-lg bg-gray-100 flex items-center justify-center animate-pulse">
            <Database className="w-5 h-5 text-gray-400 animate-spin" />
          </div>
          <p className="text-sm text-gray-500">加载中...</p>
        </div>
      ) : sources.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.5 }}
          className="text-center py-12"
        >
          <div className="w-16 h-16 mx-auto mb-4 rounded-xl bg-gray-100 flex items-center justify-center">
            <Plus className="w-8 h-8 text-gray-400" />
          </div>
          <p className="text-sm font-medium text-gray-700 mb-1">暂无信息源</p>
          <p className="text-xs text-gray-500">点击右上角"+"按钮创建新的信息源</p>
        </motion.div>
      ) : (
        <motion.div
          className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.5, delay: 0.2 }}
        >
          {sources.map((source: Source, index: number) => (
            <SourceCard
              key={source.id}
              source={source}
              documentCount={source.document_count}
              onEdit={handleEdit}
              onDelete={handleDelete}
              index={index}
            />
          ))}
        </motion.div>
      )}

      {/* 对话框 */}
      <SourceDialog
        open={isCreateDialogOpen}
        onClose={() => setIsCreateDialogOpen(false)}
        onSubmit={handleCreate}
        isLoading={createMutation.isPending}
      />

      <SourceDialog
        open={isEditDialogOpen}
        onClose={() => {
          setIsEditDialogOpen(false)
          setSelectedSource(null)
        }}
        onSubmit={handleUpdate}
        source={selectedSource}
        isLoading={updateMutation.isPending}
      />

      <DeleteConfirmDialog
        open={isDeleteDialogOpen}
        onClose={() => {
          setIsDeleteDialogOpen(false)
          setSelectedSource(null)
        }}
        onConfirm={handleConfirmDelete}
        entityType={selectedSource as any}
        isLoading={deleteMutation.isPending}
      />
    </div>
  )
}
