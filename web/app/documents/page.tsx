'use client'

import { useState, useCallback, useEffect, useMemo, Suspense } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'next/navigation'
import Link from 'next/link'
import { motion, AnimatePresence } from 'framer-motion'
import { FileText, CheckCircle, XCircle, Clock, ListTodo, Plus, ChevronRight, BookOpen, MoreVertical, Edit2, Trash2, Database, Loader2 } from 'lucide-react'
import { apiClient } from '@/lib/api-client'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { formatDate } from '@/lib/utils'
import { Document } from '@/types'
import { DocumentUploadDialog } from '@/components/documents/DocumentUploadDialog'
import { DocumentEditDialog, DocumentFormData } from '@/components/documents/DocumentEditDialog'
import { DeleteConfirmDialog } from '@/components/settings/DeleteConfirmDialog'
import { DocumentDetailDrawer } from '@/components/documents/DocumentDetailDrawer'

function DocumentsContent() {
  const searchParams = useSearchParams()
  const queryClient = useQueryClient()

  const [sourceId, setSourceId] = useState('')
  const [background, setBackground] = useState('')
  const [uploadProgress, setUploadProgress] = useState(0)
  const [isDialogOpen, setIsDialogOpen] = useState(false)

  // 抽屉状态
  const [selectedDocument, setSelectedDocument] = useState<Document | null>(null)
  const [isDetailDrawerOpen, setIsDetailDrawerOpen] = useState(false)
  const [detailDrawerView, setDetailDrawerView] = useState<'events' | 'sections'>('events')

  // 编辑和删除状态
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false)
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false)
  const [documentToEdit, setDocumentToEdit] = useState<Document | null>(null)
  const [documentToDelete, setDocumentToDelete] = useState<Document | null>(null)

  // 从URL参数获取source_config_id
  useEffect(() => {
    const sourceIdParam = searchParams.get('source_config_id')
    if (sourceIdParam) {
      setSourceId(sourceIdParam)
    }
  }, [searchParams])

  const { data: sourcesData } = useQuery({
    queryKey: ['sources'],
    queryFn: () => apiClient.getSources(),
  })

  const { data: documentsData, isLoading } = useQuery({
    queryKey: ['documents', sourceId],
    queryFn: () => sourceId ? apiClient.getDocuments(sourceId) : null,
    enabled: !!sourceId,
  })

  const uploadMutation = useMutation({
    mutationFn: ({ file, sourceId, background, entityTypes }: any) =>
      apiClient.uploadDocument(sourceId, file, true, background, entityTypes),
    onSuccess: (data, variables) => {
      // ✅ 使用 variables.sourceId 而不是闭包中的 sourceId，避免闭包陷阱
      queryClient.invalidateQueries({ queryKey: ['documents', variables.sourceId] })
      queryClient.invalidateQueries({ queryKey: ['allEntityTypes'] })  // 🆕 刷新实体类型列表
      setUploadProgress(100)
      // 上传成功后延迟关闭对话框
      setTimeout(() => {
        setIsDialogOpen(false)
        setUploadProgress(0)
      }, 1000)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: DocumentFormData }) =>
      apiClient.updateDocument(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', sourceId] })
      setIsEditDialogOpen(false)
      setDocumentToEdit(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiClient.deleteDocument(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', sourceId] })
      setIsDeleteDialogOpen(false)
      setDocumentToDelete(null)
    },
  })

  const handleEdit = (document: Document) => {
    setDocumentToEdit(document)
    setIsEditDialogOpen(true)
  }

  const handleDelete = (document: Document) => {
    setDocumentToDelete(document)
    setIsDeleteDialogOpen(true)
  }

  const handleConfirmDelete = () => {
    if (documentToDelete) {
      deleteMutation.mutate(documentToDelete.id)
    }
  }

  const handleUpdateSubmit = (data: DocumentFormData) => {
    if (documentToEdit) {
      updateMutation.mutate({ id: documentToEdit.id, data })
    }
  }

  const handleUpload = useCallback((
    file: File, 
    uploadSourceId: string, 
    uploadBackground: string,
    entityTypes?: any[]  // 🆕 实体类型配置
  ) => {
    // 模拟上传进度
    setUploadProgress(0)
    const interval = setInterval(() => {
      setUploadProgress(prev => {
        if (prev >= 90) {
          clearInterval(interval)
          return prev
        }
        return prev + 10
      })
    }, 200)

    uploadMutation.mutate({ 
      file, 
      sourceId: uploadSourceId, 
      background: uploadBackground,
      entityTypes  // 🆕 传递实体类型配置
    })
  }, [uploadMutation])

  const documents = useMemo(() => {
    return (documentsData?.data || []) as Document[]
  }, [documentsData])

  // 当文档数据加载完成后，检查是否需要自动弹出上传对话框
  // 只有当信息源没有文档时才自动弹出
  useEffect(() => {
    const sourceIdParam = searchParams.get('source_config_id')
    if (sourceIdParam && documentsData && documents.length === 0 && !isDialogOpen) {
      uploadMutation.reset() // 重置上传状态
      setIsDialogOpen(true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentsData, documents.length, searchParams, isDialogOpen])

  // 当有 PROCESSING 状态的文档时，每 3 秒刷新一次
  useEffect(() => {
    const hasProcessing = documents.some(doc => doc.status === 'PROCESSING')
    if (hasProcessing && sourceId) {
      const interval = setInterval(() => {
        queryClient.invalidateQueries({ queryKey: ['documents', sourceId] })
      }, 3000)
      return () => clearInterval(interval)
    }
  }, [documents, sourceId, queryClient])

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'COMPLETED':
        return (
          <div className="p-1.5 rounded-lg shrink-0 bg-emerald-50">
            <CheckCircle className="w-4 h-4 text-emerald-600" />
          </div>
        )
      case 'PROCESSING':
        return (
          <div className="p-1.5 rounded-lg shrink-0 bg-amber-50">
            <Loader2 className="w-4 h-4 text-amber-600 animate-spin" />
          </div>
        )
      case 'FAILED':
        return (
          <div className="p-1.5 rounded-lg shrink-0 bg-gray-50">
            <XCircle className="w-4 h-4 text-gray-400" />
          </div>
        )
      default:
        return (
          <div className="p-1.5 rounded-lg shrink-0 bg-gray-50">
            <Clock className="w-4 h-4 text-gray-400" />
          </div>
        )
    }
  }

  return (
    <div className="space-y-8">
      {/* 面包屑导航 */}
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3 }}
        className="flex items-center gap-2 text-sm"
      >
        <Link 
          href="/sources" 
          className="text-gray-500 hover:text-gray-700 transition-colors flex items-center gap-1"
        >
          <Database className="w-4 h-4" />
          <span>信息源管理</span>
        </Link>
        <ChevronRight className="w-4 h-4 text-gray-400" />
        <span className="text-gray-900 font-medium flex items-center gap-1">
          <FileText className="w-4 h-4" />
          <span>文档管理</span>
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
          <div className="p-2.5 rounded-xl bg-emerald-50 shadow-md">
            <FileText className="w-6 h-6 text-emerald-600" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-800">
              文档管理
            </h1>
            <p className="text-gray-500 text-xs">上传和管理文档，自动执行解析</p>
          </div>
        </div>
        <button
          onClick={() => {
            uploadMutation.reset() // 重置上传状态
            setIsDialogOpen(true)
          }}
          className="h-11 w-11 p-0 rounded-full bg-gray-100 text-gray-600 hover:bg-gray-200 shadow-lg hover:shadow-xl transition-all flex items-center justify-center"
        >
          <Plus className="w-5 h-5" />
        </button>
      </motion.div>

      {/* 文档列表 */}
      {sourceId && (
        <>
          {isLoading ? (
            <div className="text-center py-12">
              <div className="text-sm text-gray-500">加载中...</div>
            </div>
          ) : documents.length === 0 ? (
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: 0.5 }}
              className="flex flex-col items-center justify-center py-24"
            >
              <div className="flex flex-col items-center text-center space-y-3">
                <div className="p-4 rounded-2xl bg-blue-50/50">
                  <FileText className="w-12 h-12 text-blue-300" />
                </div>
                <div className="space-y-1">
                  <p className="text-sm font-medium text-gray-900">暂无文档</p>
                  <p className="text-xs text-gray-500">点击右上角按钮上传文档</p>
                </div>
              </div>
            </motion.div>
          ) : (
            <motion.div
              className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.5, delay: 0.2 }}
            >
              {documents.map((doc: Document, index: number) => (
                <motion.div
                  key={doc.id}
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3, delay: index * 0.1 }}
                  whileHover={{ y: -5 }}
                >
                  <Card className="relative border-0 rounded-lg p-6 bg-white/80 backdrop-blur-sm shadow-lg hover:shadow-xl transition-all duration-300 group h-full">
                    {/* 右上角：操作菜单 */}
                    <div className="absolute top-4 right-4 flex items-center gap-2">
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
                            onClick={() => handleEdit(doc)}
                            className="cursor-pointer text-sm"
                          >
                            <Edit2 className="w-4 h-4 mr-2" />
                            编辑
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            onClick={() => handleDelete(doc)}
                            className="cursor-pointer text-sm text-red-600 focus:text-red-600"
                          >
                            <Trash2 className="w-4 h-4 mr-2" />
                            删除
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>

                    <CardContent className="p-0">
                      {/* 垂直布局：图标 + 内容 */}
                      <div className="space-y-4">
                        {/* 顶部：状态图标 + 文档标题 */}
                        <div className="flex items-center gap-3 mb-2">
                          {getStatusIcon(doc.status)}
                          <div className="flex-1 min-w-0">
                            <h3 className="font-semibold text-lg text-gray-900 truncate">
                              {doc.title}
                            </h3>
                          </div>
                        </div>

                        {/* 描述 */}
                        {doc.summary && (
                          <p className="text-sm text-gray-600 line-clamp-2">
                            {doc.summary}
                          </p>
                        )}

                        {/* 统计信息 */}
                        <div className="space-y-2 text-sm">
                          {/* 片段 - 可点击 */}
                          <button
                            onClick={() => {
                              setSelectedDocument(doc)
                              setDetailDrawerView('sections')
                              setIsDetailDrawerOpen(true)
                            }}
                            className="w-full flex items-center justify-between px-3 py-2 hover:bg-gray-50 rounded-lg transition-colors group"
                          >
                            <span className="text-gray-500 flex items-center">
                              <BookOpen className="w-3.5 h-3.5 mr-2" />
                              片段
                            </span>
                            <div className="flex items-center gap-1.5">
                              <span className="font-medium text-gray-700">{doc.sections_count}</span>
                              <ChevronRight className="w-4 h-4 text-gray-400 group-hover:text-gray-600 transition-colors" />
                            </div>
                          </button>

                          {/* 事项 - 可点击 */}
                          <button
                            onClick={() => {
                              setSelectedDocument(doc)
                              setDetailDrawerView('events')
                              setIsDetailDrawerOpen(true)
                            }}
                            className="w-full flex items-center justify-between px-3 py-2 hover:bg-gray-50 rounded-lg transition-colors group"
                          >
                            <span className="text-gray-500 flex items-center">
                              <ListTodo className="w-3.5 h-3.5 mr-2" />
                              事项
                            </span>
                            <div className="flex items-center gap-1.5">
                              <span className="font-medium text-gray-700">{doc.events_count}</span>
                              <ChevronRight className="w-4 h-4 text-gray-400 group-hover:text-gray-600 transition-colors" />
                            </div>
                          </button>

                          {/* 创建时间 */}
                          <div className="flex items-center justify-between px-3 py-2">
                            <span className="text-gray-500">创建时间:</span>
                            <span className="font-medium text-gray-700">{formatDate(doc.created_time)}</span>
                          </div>
                        </div>

                        {/* 标签 */}
                        {doc.tags && doc.tags.length > 0 && (
                          <div className="flex gap-1.5 flex-wrap pt-2">
                            {doc.tags.map((tag, i) => (
                              <Badge key={i} variant="outline" className="text-xs px-2 py-1 bg-gray-50 text-gray-600 border-gray-200">
                                {tag}
                              </Badge>
                            ))}
                          </div>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                </motion.div>
              ))}
            </motion.div>
          )}
        </>
      )}

      {/* 上传对话框 */}
      <DocumentUploadDialog
        open={isDialogOpen}
        onClose={() => setIsDialogOpen(false)}
        onUpload={handleUpload}
        sources={sourcesData?.data || []}
        defaultSourceId={sourceId}
        uploadProgress={uploadProgress}
        isUploading={uploadMutation.isPending}
        uploadSuccess={uploadMutation.isSuccess}
        uploadError={uploadMutation.isError}
      />

      {/* 文档详情抽屉（统一事项和片段） */}
      <DocumentDetailDrawer
        open={isDetailDrawerOpen}
        onClose={() => setIsDetailDrawerOpen(false)}
        articleId={selectedDocument?.id}
        defaultView={detailDrawerView}
      />

      {/* 编辑对话框 */}
      <DocumentEditDialog
        open={isEditDialogOpen}
        onClose={() => {
          setIsEditDialogOpen(false)
          setDocumentToEdit(null)
        }}
        onSubmit={handleUpdateSubmit}
        document={documentToEdit}
        isLoading={updateMutation.isPending}
      />

      {/* 删除确认对话框 */}
      <DeleteConfirmDialog
        open={isDeleteDialogOpen}
        onClose={() => {
          setIsDeleteDialogOpen(false)
          setDocumentToDelete(null)
        }}
        onConfirm={handleConfirmDelete}
        entityType={documentToDelete as any}
        isLoading={deleteMutation.isPending}
      />
    </div>
  )
}

// Loading fallback component
function DocumentsLoadingFallback() {
  return (
    <div className="space-y-8">
      {/* 面包屑导航 */}
      <div className="flex items-center gap-2 text-sm">
        <Link 
          href="/sources" 
          className="text-gray-500 hover:text-gray-700 transition-colors flex items-center gap-1"
        >
          <Database className="w-4 h-4" />
          <span>信息源管理</span>
        </Link>
        <ChevronRight className="w-4 h-4 text-gray-400" />
        <span className="text-gray-900 font-medium flex items-center gap-1">
          <FileText className="w-4 h-4" />
          <span>文档管理</span>
        </span>
      </div>

      <div className="flex justify-between items-center">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-emerald-50 shadow-md">
            <FileText className="w-6 h-6 text-emerald-600" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-800">文档管理</h1>
            <p className="text-gray-500 text-xs">上传和管理文档，自动执行解析</p>
          </div>
        </div>
      </div>
      <div className="text-center py-12">
        <div className="text-sm text-gray-500">加载中...</div>
      </div>
    </div>
  )
}

// Main page component with Suspense boundary
export default function DocumentsPage() {
  return (
    <Suspense fallback={<DocumentsLoadingFallback />}>
      <DocumentsContent />
    </Suspense>
  )
}
