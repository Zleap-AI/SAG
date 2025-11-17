'use client'

import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Activity, Clock, CheckCircle, XCircle, Loader2, RefreshCw, Trash2,
  Search, Copy, ExternalLink, ChevronLeft, ChevronRight, X
} from 'lucide-react'
import { motion } from 'framer-motion'
import Link from 'next/link'
import { apiClient } from '@/lib/api-client'
import { Card, CardContent } from '@/components/ui/card'
import { ClearTasksDialog } from '@/components/tasks/ClearTasksDialog'
import { Task, TaskStats } from '@/types'

interface PaginatedTasksResponse {
  success: boolean
  data: Task[]
  pagination: {
    total: number
    page: number
    page_size: number
    total_pages: number
  }
}
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { formatDistanceToNow, differenceInMinutes, differenceInSeconds } from 'date-fns'
import { zhCN } from 'date-fns/locale'
import { toast } from 'sonner'

export default function TasksPage() {
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [searchInput, setSearchInput] = useState('')  // 输入框的值
  const [searchQuery, setSearchQuery] = useState('')  // 实际发送给后端的搜索值（防抖后）
  const [isClearDialogOpen, setIsClearDialogOpen] = useState(false)
  const [currentPage, setCurrentPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const queryClient = useQueryClient()

  // 🔍 搜索防抖：500ms 后才触发真正的搜索
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearchQuery(searchInput)
      setCurrentPage(1)  // 搜索时重置到第一页
    }, 500)

    return () => clearTimeout(timer)
  }, [searchInput])

  // 获取任务列表（使用后端分页和搜索）
  const { data: tasksData, isLoading, refetch } = useQuery<PaginatedTasksResponse>({
    queryKey: ['tasks', statusFilter, searchQuery, currentPage, pageSize],
    queryFn: async () => {
      const params: Record<string, string | number> = {
        page: currentPage,
        page_size: pageSize,
      }
      if (statusFilter !== 'all') params.status = statusFilter
      if (searchQuery) params.search = searchQuery
      return apiClient.getTasks(params) as unknown as Promise<PaginatedTasksResponse>
    },
    refetchInterval: (query) => {
      // 智能轮询：仅对近20分钟内的处理中任务轮询
      const tasks = query.state?.data?.data || []
      const now = new Date()
      const THRESHOLD_MINUTES = 20
      const POLLING_INTERVAL = 5000 // 5秒

      // 筛选处理中的任务
      const processingTasks = tasks.filter((t: Task) =>
        t.status === 'processing' || t.status === 'pending'
      )

      // 检查是否有近20分钟内的处理中任务
      const hasRecentProcessing = processingTasks.some((task: Task) => {
        // 优先使用 updated_time，回退到 created_time
        const timeStr = task.updated_time || task.created_time
        if (!timeStr) {
          // 如果时间字段为空，保守处理：认为是最近的任务，继续轮询
          console.warn(`Task ${task.task_id} has no timestamp, treating as recent`)
          return true
        }

        try {
          const taskTime = new Date(timeStr)
          // 检查日期是否有效
          if (isNaN(taskTime.getTime())) {
            console.error(`Invalid date for task ${task.task_id}: ${timeStr}`)
            return false
          }

          const minutesSince = differenceInMinutes(now, taskTime)
          return minutesSince <= THRESHOLD_MINUTES
        } catch (error) {
          console.error(`Error parsing date for task ${task.task_id}:`, error)
          return false
        }
      })

      // 如果有近20分钟内的处理中任务，5秒轮询；否则停止轮询
      return hasRecentProcessing ? POLLING_INTERVAL : false
    },
  })

  // 获取统计信息
  const { data: statsData, isLoading: statsLoading } = useQuery({
    queryKey: ['tasks-stats'],
    queryFn: () => apiClient.getTasksStats(),
    refetchInterval: 5000,
  })

  // 批量删除 mutation
  const batchDeleteMutation = useMutation({
    mutationFn: (status_filter: string[]) =>
      apiClient.batchDeleteTasks({ status_filter }),
    onSuccess: (response) => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['tasks-stats'] })
      setIsClearDialogOpen(false)
      toast.success(response.data.message || '删除成功')
    },
    onError: () => {
      toast.error('删除失败，请稍后重试')
    },
  })

  const tasks = tasksData?.data || []
  const pagination = tasksData?.pagination || { total: 0, page: 1, page_size: 10, total_pages: 1 }
  const total = pagination.total
  const totalPages = pagination.total_pages
  const stats: TaskStats | undefined = statsData?.data?.data

  // 当筛选条件变化时，重置到第一页
  const resetPage = () => {
    setCurrentPage(1)
  }

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'completed':
        return <CheckCircle className="w-4 h-4 text-green-600" />
      case 'failed':
        return <XCircle className="w-4 h-4 text-red-600" />
      case 'processing':
      case 'pending':
        return <Loader2 className="w-4 h-4 text-blue-600 animate-spin" />
      default:
        return <Clock className="w-4 h-4 text-gray-400" />
    }
  }

  const getStatusBadge = (status: string) => {
    const styles = {
      completed: 'bg-green-100 text-green-800',
      failed: 'bg-red-100 text-red-800',
      processing: 'bg-blue-100 text-blue-800',
      pending: 'bg-yellow-100 text-yellow-800',
      cancelled: 'bg-gray-100 text-gray-800',
    }
    const labels = {
      completed: '已完成',
      failed: '失败',
      processing: '处理中',
      pending: '等待中',
      cancelled: '已取消',
    }
    return (
      <span className={`px-2 py-1 rounded-full text-xs font-medium ${styles[status as keyof typeof styles] || styles.cancelled}`}>
        {labels[status as keyof typeof labels] || status}
      </span>
    )
  }

  const getTaskTypeBadge = (taskType?: string) => {
    const styles = {
      document_upload: 'bg-blue-100 text-blue-800',
      pipeline_run: 'bg-indigo-100 text-indigo-800',
    }
    const labels = {
      document_upload: '文档上传',
      pipeline_run: 'Pipeline',
    }
    if (!taskType) return null
    return (
      <span className={`px-2 py-1 rounded text-xs font-medium ${styles[taskType as keyof typeof styles] || 'bg-gray-100 text-gray-800'}`}>
        {labels[taskType as keyof typeof labels] || taskType}
      </span>
    )
  }

  const handleClearCompleted = () => {
    setIsClearDialogOpen(true)
  }

  const handleConfirmClear = () => {
    batchDeleteMutation.mutate(['completed', 'failed'])
  }

  const copyTaskId = (taskId: string) => {
    if (navigator?.clipboard?.writeText) {
      navigator.clipboard.writeText(taskId)
        .then(() => {
          toast.success('任务ID已复制到剪贴板')
        })
        .catch(() => {
          // 降级方案：使用传统方法
          fallbackCopyTextToClipboard(taskId)
        })
    } else {
      // 浏览器不支持 clipboard API，使用降级方案
      fallbackCopyTextToClipboard(taskId)
    }
  }

  const fallbackCopyTextToClipboard = (text: string) => {
    const textArea = document.createElement("textarea")
    textArea.value = text
    textArea.style.position = "fixed"
    textArea.style.top = "0"
    textArea.style.left = "0"
    textArea.style.opacity = "0"
    document.body.appendChild(textArea)
    textArea.focus()
    textArea.select()
    try {
      document.execCommand('copy')
      toast.success('任务ID已复制到剪贴板')
    } catch {
      toast.error('复制失败，请手动复制')
    }
    document.body.removeChild(textArea)
  }

  // 计算任务耗时
  const calculateDuration = (task: Task): string => {
    if (!task.created_time) return '-'
    
    const startTime = new Date(task.created_time)
    let endTime: Date
    
    // 如果任务已完成或失败，使用 updated_time；否则使用当前时间
    if (task.status === 'completed' || task.status === 'failed') {
      endTime = task.updated_time ? new Date(task.updated_time) : new Date()
    } else {
      endTime = new Date()
    }
    
    const seconds = differenceInSeconds(endTime, startTime)
    
    if (seconds < 0) return '-'
    
    // 格式化耗时
    if (seconds < 60) {
      return `${seconds} 秒`
    } else if (seconds < 3600) {
      const minutes = Math.floor(seconds / 60)
      const remainingSeconds = seconds % 60
      return remainingSeconds > 0 ? `${minutes} 分 ${remainingSeconds} 秒` : `${minutes} 分钟`
    } else {
      const hours = Math.floor(seconds / 3600)
      const minutes = Math.floor((seconds % 3600) / 60)
      return minutes > 0 ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`
    }
  }

  return (
    <div className="space-y-6">
      {/* 标题和工具栏 */}
      <div className="flex items-center justify-between">
        <motion.div
          className="flex items-center gap-3"
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <div className="p-2.5 rounded-xl bg-gradient-to-br from-pink-100 to-rose-100 shadow-md">
            <Activity className="w-6 h-6 text-pink-600" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-800">任务监控</h1>
            <p className="text-gray-500 text-xs">实时查看任务执行状态和进度</p>
          </div>
        </motion.div>

        {/* 工具栏按钮 */}
        <TooltipProvider>
          <div className="flex items-center gap-2">
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={() => refetch()}
                  disabled={isLoading}
                  className="h-11 w-11 p-0 rounded-full bg-gray-100 text-gray-600 hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed shadow-lg hover:shadow-xl transition-all flex items-center justify-center"
                >
                  <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
                </button>
              </TooltipTrigger>
              <TooltipContent>
                <p>刷新任务列表</p>
              </TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <motion.button
                  onClick={handleClearCompleted}
                  disabled={batchDeleteMutation.isPending || !tasks.length}
                  className="h-11 w-11 p-0 rounded-full bg-gray-100 text-gray-600 hover:bg-red-100 hover:text-red-600 disabled:opacity-50 disabled:cursor-not-allowed shadow-lg hover:shadow-xl transition-all flex items-center justify-center"
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                >
                  <Trash2 className="w-4 h-4" />
                </motion.button>
              </TooltipTrigger>
              <TooltipContent>
                <p>清空已完成和失败的任务</p>
              </TooltipContent>
            </Tooltip>
          </div>
        </TooltipProvider>
      </div>

      {/* 统计卡片 - 始终显示 */}
      {!statsLoading && stats && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <Card>
            <CardContent className="p-4">
              <div className="text-xs text-gray-500">总计</div>
              <div className="text-2xl font-bold">{stats.total}</div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <div className="text-xs text-yellow-600">等待中</div>
              <div className="text-2xl font-bold text-yellow-600">{stats.by_status.pending}</div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <div className="text-xs text-blue-600">处理中</div>
              <div className="text-2xl font-bold text-blue-600">{stats.by_status.processing}</div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <div className="text-xs text-green-600">已完成</div>
              <div className="text-2xl font-bold text-green-600">{stats.by_status.completed}</div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <div className="text-xs text-red-600">失败</div>
              <div className="text-2xl font-bold text-red-600">{stats.by_status.failed}</div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* 筛选和搜索栏 */}
      <Card>
        <CardContent className="p-4">
          <div className="flex flex-wrap gap-3">
            <div className="flex-1 min-w-[200px]">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
                <Input
                  placeholder="搜索任务ID、消息、信息源、文档..."
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  className="pl-10 pr-10 focus:border-pink-300 focus:ring-pink-500"
                />
                {searchInput && (
                  <button
                    onClick={() => setSearchInput('')}
                    className="absolute right-3 top-1/2 transform -translate-y-1/2 text-gray-400 hover:text-gray-600 transition-colors"
                  >
                    <X className="w-4 h-4" />
                  </button>
                )}
              </div>
            </div>
            <Select value={statusFilter} onValueChange={(value) => {
              setStatusFilter(value)
              resetPage()
            }}>
              <SelectTrigger className="w-[180px] hover:border-pink-300 focus:ring-pink-500">
                <SelectValue placeholder="状态" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all" className="hover:bg-pink-50 focus:bg-pink-50">全部状态</SelectItem>
                <SelectItem value="pending" className="hover:bg-pink-50 focus:bg-pink-50">等待中</SelectItem>
                <SelectItem value="processing" className="hover:bg-pink-50 focus:bg-pink-50">处理中</SelectItem>
                <SelectItem value="completed" className="hover:bg-pink-50 focus:bg-pink-50">已完成</SelectItem>
                <SelectItem value="failed" className="hover:bg-pink-50 focus:bg-pink-50">失败</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      {/* 任务表格 */}
      {isLoading ? (
        <div className="text-center py-12">
          <Loader2 className="w-8 h-8 mx-auto animate-spin text-gray-400" />
          <p className="text-gray-500 mt-4 text-sm">加载中...</p>
        </div>
      ) : tasks.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center">
            <Activity className="w-12 h-12 mx-auto text-gray-400 mb-3" />
            <p className="text-gray-500 text-sm">
              {searchQuery ? '没有找到匹配的任务' : '暂无任务'}
            </p>
            {searchQuery && (
              <p className="text-gray-400 text-xs mt-2">尝试修改搜索关键词或清除筛选条件</p>
            )}
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[100px]">任务ID</TableHead>
                  <TableHead>类型</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="w-[200px]">进度</TableHead>
                  <TableHead>消息</TableHead>
                  <TableHead className="w-[200px]">关联信息</TableHead>
                  <TableHead className="w-[100px]">创建时间</TableHead>
                  <TableHead className="w-[100px]">完成时间</TableHead>
                  <TableHead className="w-[100px]">耗时</TableHead>
                  <TableHead className="w-[80px] text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tasks.map((task: Task) => (
                  <TableRow key={task.task_id}>
                    <TableCell className="font-mono text-xs">
                      {task.task_id.slice(0, 8)}
                    </TableCell>
                    <TableCell>
                      {getTaskTypeBadge(task.task_type)}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        {getStatusIcon(task.status)}
                        {getStatusBadge(task.status)}
                      </div>
                    </TableCell>
                    <TableCell>
                      {task.progress !== undefined && task.progress !== null && (
                        <div className="space-y-1">
                          <div className="flex justify-between text-xs text-gray-600">
                            <span>{Math.round(task.progress)}%</span>
                          </div>
                          <Progress value={task.progress} className="h-2" />
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="max-w-[300px]">
                      <div className="truncate text-sm">{task.message}</div>
                      {task.error && (
                        <div className="text-xs text-red-600 truncate mt-1">{task.error}</div>
                      )}
                    </TableCell>
                    <TableCell className="max-w-[200px]">
                      <TooltipProvider>
                        <div className="space-y-1">
                          {task.source_name && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Link
                                  href={`/documents?source_config_id=${task.source_config_id}`}
                                  className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800 hover:underline transition-all duration-200"
                                >
                                  <span className="truncate">{task.source_name}</span>
                                  <ExternalLink className="w-3 h-3 flex-shrink-0" />
                                </Link>
                              </TooltipTrigger>
                              <TooltipContent>
                                <p>查看信息源: {task.source_name}</p>
                              </TooltipContent>
                            </Tooltip>
                          )}
                          {task.article_title && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Link
                                  href={`/documents?source_config_id=${task.source_config_id}&article_id=${task.article_id}`}
                                  className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800 hover:underline transition-all duration-200"
                                >
                                  <span className="truncate">{task.article_title}</span>
                                  <ExternalLink className="w-3 h-3 flex-shrink-0" />
                                </Link>
                              </TooltipTrigger>
                              <TooltipContent>
                                <p>查看文档: {task.article_title}</p>
                              </TooltipContent>
                            </Tooltip>
                          )}
                          {!task.source_name && !task.article_title && (
                            <span className="text-xs text-gray-400">-</span>
                          )}
                        </div>
                      </TooltipProvider>
                    </TableCell>
                    <TableCell className="text-xs text-gray-500">
                      {/* UTC timestamps from backend (with 'Z' suffix) are automatically converted to local timezone */}
                      {task.created_time && formatDistanceToNow(new Date(task.created_time), {
                        addSuffix: true,
                        locale: zhCN
                      })}
                    </TableCell>
                    <TableCell className="text-xs text-gray-500">
                      {/* Show completion time only for finished tasks */}
                      {task.updated_time && (task.status === 'completed' || task.status === 'failed') && formatDistanceToNow(new Date(task.updated_time), {
                        addSuffix: true,
                        locale: zhCN
                      })}
                    </TableCell>
                    <TableCell className="text-xs text-gray-500">
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className={`cursor-help ${
                              task.status === 'processing' || task.status === 'pending' 
                                ? 'text-blue-600 font-medium' 
                                : 'text-gray-600'
                            }`}>
                              {calculateDuration(task)}
                            </span>
                          </TooltipTrigger>
                          <TooltipContent>
                            <p>
                              {task.status === 'processing' || task.status === 'pending' 
                                ? '任务进行中，实时计算耗时' 
                                : '任务总耗时'}
                            </p>
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    </TableCell>
                    <TableCell className="text-right">
                      <TooltipProvider>
                        <div className="flex items-center justify-end gap-1">
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => copyTaskId(task.task_id)}
                                className="h-8 w-8 p-0"
                              >
                                <Copy className="w-3.5 h-3.5" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>
                              <p>复制任务ID</p>
                            </TooltipContent>
                          </Tooltip>
                        </div>
                      </TooltipProvider>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {/* 分页控件 */}
      {!isLoading && total > 0 && totalPages > 1 && (
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center justify-between">
              {/* 左侧：任务统计 */}
              <div className="text-sm text-gray-600">
                第 {currentPage} 页 / 共 {totalPages} 页，总计 {total} 条任务
              </div>

              {/* 中间：页码 */}
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}
                  disabled={currentPage === 1}
                  className="h-8 w-8 p-0 hover:bg-pink-50 hover:text-pink-600 hover:border-pink-300"
                >
                  <ChevronLeft className="w-4 h-4" />
                </Button>

                {/* 页码显示 */}
                <div className="flex items-center gap-1">
                  {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
                    let pageNum: number
                    if (totalPages <= 5) {
                      pageNum = i + 1
                    } else if (currentPage <= 3) {
                      pageNum = i + 1
                    } else if (currentPage >= totalPages - 2) {
                      pageNum = totalPages - 4 + i
                    } else {
                      pageNum = currentPage - 2 + i
                    }
                    
                    return (
                      <Button
                        key={pageNum}
                        variant="outline"
                        size="sm"
                        onClick={() => setCurrentPage(pageNum)}
                        className={`h-8 w-8 p-0 ${
                          currentPage === pageNum 
                            ? 'bg-pink-500 text-white border-pink-500 hover:bg-pink-600' 
                            : 'hover:bg-pink-50 hover:text-pink-600 hover:border-pink-300'
                        }`}
                      >
                        {pageNum}
                      </Button>
                    )
                  })}
                </div>

                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage(Math.min(totalPages, currentPage + 1))}
                  disabled={currentPage === totalPages}
                  className="h-8 w-8 p-0 hover:bg-pink-50 hover:text-pink-600 hover:border-pink-300"
                >
                  <ChevronRight className="w-4 h-4" />
                </Button>
              </div>

              {/* 右侧：每页显示数量 */}
              <Select value={pageSize.toString()} onValueChange={(value) => {
                setPageSize(Number(value))
                resetPage()
              }}>
                <SelectTrigger className="w-[120px] h-8 hover:border-pink-300 focus:ring-pink-500">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="10" className="hover:bg-pink-50 focus:bg-pink-50">10 条/页</SelectItem>
                  <SelectItem value="20" className="hover:bg-pink-50 focus:bg-pink-50">20 条/页</SelectItem>
                  <SelectItem value="50" className="hover:bg-pink-50 focus:bg-pink-50">50 条/页</SelectItem>
                  <SelectItem value="100" className="hover:bg-pink-50 focus:bg-pink-50">100 条/页</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 清空任务确认弹框 */}
      <ClearTasksDialog
        open={isClearDialogOpen}
        onClose={() => setIsClearDialogOpen(false)}
        onConfirm={handleConfirmClear}
        isLoading={batchDeleteMutation.isPending}
      />
    </div>
  )
}
