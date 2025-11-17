'use client'

import { useState, useCallback, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Upload, Database, ChevronDown, Plus, Trash2 } from 'lucide-react'
import { useDropzone } from 'react-dropzone'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Progress } from '@/components/ui/progress'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Label } from '@/components/ui/label'

// 实体类型快捷创建数据结构
interface QuickEntityType {
  id: string
  type: string
  name: string
  description: string
  weight: number
  similarity_threshold: number
  value_constraints?: {
    type?: 'int' | 'float' | 'datetime' | 'bool' | 'enum' | 'text'
    enum_values?: string[]
    unit?: string
    default?: any
    override?: boolean
  }
}

interface DocumentUploadDialogProps {
  open: boolean
  onClose: () => void
  onUpload: (file: File, sourceId: string, background: string, entityTypes?: QuickEntityType[]) => void
  sources: any[]
  defaultSourceId?: string
  uploadProgress: number
  isUploading: boolean
  uploadSuccess: boolean
  uploadError: boolean
}

export function DocumentUploadDialog({
  open,
  onClose,
  onUpload,
  sources,
  defaultSourceId = '',
  uploadProgress,
  isUploading,
  uploadSuccess,
  uploadError,
}: DocumentUploadDialogProps) {
  const [sourceId, setSourceId] = useState(defaultSourceId)
  const [background, setBackground] = useState('')
  const [isAdvancedOpen, setIsAdvancedOpen] = useState(false)
  const [entityTypes, setEntityTypes] = useState<QuickEntityType[]>([])

  // 当弹框打开时重置状态
  useEffect(() => {
    if (open) {
      // 重置 background 字段
      setBackground('')
      // 设置默认 sourceId
      if (defaultSourceId) {
        setSourceId(defaultSourceId)
      }
      // 重置实体类型列表
      setEntityTypes([])
      setIsAdvancedOpen(false)
    }
  }, [open, defaultSourceId])

  // 添加实体类型
  const handleAddEntityType = () => {
    const newEntityType: QuickEntityType = {
      id: `temp-${Date.now()}`,
      type: '',
      name: '',
      description: '',
      weight: 1.0,
      similarity_threshold: 0.8,
      value_constraints: undefined,
    }
    setEntityTypes([...entityTypes, newEntityType])
  }

  // 应用快捷模板
  const applyTemplate = (id: string, template: 'price' | 'stage' | 'date' | 'status') => {
    const templates = {
      price: {
        value_constraints: {
          type: 'float' as const,
          unit: '元',
        }
      },
      stage: {
        value_constraints: {
          type: 'enum' as const,
          enum_values: ['需求分析', '设计', '开发', '测试', '上线'],
        }
      },
      date: {
        value_constraints: {
          type: 'datetime' as const,
        }
      },
      status: {
        value_constraints: {
          type: 'enum' as const,
          enum_values: ['待处理', '进行中', '已完成', '已取消'],
        }
      },
    }
    
    const config = templates[template]
    setEntityTypes(entityTypes.map(et => 
      et.id === id ? { ...et, ...config } : et
    ))
  }

  // 删除实体类型
  const handleRemoveEntityType = (id: string) => {
    setEntityTypes(entityTypes.filter(et => et.id !== id))
  }

  // 更新实体类型字段
  const handleUpdateEntityType = (id: string, field: keyof QuickEntityType, value: any) => {
    setEntityTypes(entityTypes.map(et => 
      et.id === id ? { ...et, [field]: value } : et
    ))
  }

  const onDrop = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0 && sourceId) {
      // 过滤掉空的实体类型
      const validEntityTypes = entityTypes.filter(et => et.type.trim() && et.name.trim())
      
      acceptedFiles.forEach((file) => {
        onUpload(file, sourceId, background, validEntityTypes.length > 0 ? validEntityTypes : undefined)
      })
    }
  }, [sourceId, background, entityTypes, onUpload])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      // Markdown
      'text/markdown': ['.md', '.markdown'],
      // Office 文档
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document': ['.docx'],
      'application/msword': ['.doc'],
      'application/vnd.openxmlformats-officedocument.presentationml.presentation': ['.pptx'],
      'application/vnd.ms-powerpoint': ['.ppt'],
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
      'application/vnd.ms-excel': ['.xls'],
      // PDF
      'application/pdf': ['.pdf'],
      // 网页
      'text/html': ['.html', '.htm'],
      // 图片
      'image/png': ['.png'],
      'image/jpeg': ['.jpg', '.jpeg'],
      // 其他
      'text/plain': ['.txt'],
      'application/json': ['.json'],
      'text/csv': ['.csv'],
      'application/xml': ['.xml'],
      'text/xml': ['.xml'],
    },
    disabled: !sourceId || isUploading,
  })

  if (!open) return null

  return (
    <AnimatePresence>
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        {/* 背景遮罩 */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
          onClick={onClose}
          className="absolute inset-0 bg-black/20 backdrop-blur-md"
        />

        {/* 对话框内容 */}
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 20 }}
          transition={{ type: 'spring', stiffness: 380, damping: 30 }}
          className="relative w-full max-w-lg bg-white rounded-2xl shadow-2xl overflow-hidden"
        >
          {/* 头部 */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
            <h2 className="text-lg font-semibold text-gray-800">
              上传文档
            </h2>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors"
            >
              <X className="w-5 h-5 text-gray-500" />
            </button>
          </div>

          {/* 表单 */}
          <div className="px-6 py-5 space-y-5">
            {/* 信息源选择 */}
            <div className="space-y-2.5">
              <label htmlFor="source-select" className="text-sm font-medium text-gray-800">
                选择信息源 <span className="text-red-500">*</span>
              </label>
              <Select value={sourceId} onValueChange={setSourceId}>
                <SelectTrigger id="source-select" className="h-11 rounded-lg border-2 border-gray-200 hover:border-gray-300 focus:border-gray-500 transition-all">
                  <SelectValue placeholder="选择信息源..." />
                </SelectTrigger>
                <SelectContent className="bg-white border-gray-200 shadow-lg z-[100]">
                  {sources?.map((source: any) => (
                    <SelectItem key={source.id} value={source.id} className="cursor-pointer">
                      <div className="flex items-center space-x-2">
                        <Database className="w-4 h-4" />
                        <span>{source.name}</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* 背景信息 */}
            <div className="space-y-2.5">
              <label htmlFor="background-input" className="text-sm font-medium text-gray-800">
                背景信息（可选）
              </label>
              <textarea
                id="background-input"
                value={background}
                onChange={(e) => setBackground(e.target.value)}
                placeholder="例如：AI技术文档集合"
                rows={3}
                className="w-full px-3 py-2 resize-none rounded-lg border-2 border-gray-200 hover:border-gray-300 focus:border-gray-500 transition-all"
              />
            </div>

            {/* 高级设置 - 快捷创建文档专属属性 */}
            <Collapsible open={isAdvancedOpen} onOpenChange={setIsAdvancedOpen}>
              <CollapsibleTrigger className="flex items-center justify-between w-full p-3 bg-gray-50 hover:bg-gray-100 rounded-lg transition-colors">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-800">高级设置</span>
                  <span className="text-xs text-gray-500">(可选)</span>
                </div>
                <ChevronDown
                  className={`w-4 h-4 text-gray-500 transition-transform ${
                    isAdvancedOpen ? 'transform rotate-180' : ''
                  }`}
                />
              </CollapsibleTrigger>

              <CollapsibleContent className="space-y-4 pt-4">
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <label className="text-sm font-medium text-gray-700">
                      文档专属属性
                    </label>
                    <button
                      type="button"
                      onClick={handleAddEntityType}
                      className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-blue-600 bg-blue-50 rounded-md hover:bg-blue-100 transition-colors"
                    >
                      <Plus className="w-3.5 h-3.5" />
                      添加属性
                    </button>
                  </div>

                  {entityTypes.length === 0 ? (
                    <p className="text-xs text-gray-500 text-center py-4 bg-gray-50 rounded-lg border border-dashed border-gray-300">
                      点击"添加属性"快速创建文档专属的数据属性
                    </p>
                  ) : (
                    <div className="space-y-3 max-h-96 overflow-y-auto pr-1">
                      {entityTypes.map((et, index) => (
                        <div
                          key={et.id}
                          className="p-3 bg-gray-50 border border-gray-200 rounded-lg space-y-2.5"
                        >
                          <div className="flex items-start justify-between gap-2">
                            <span className="text-xs font-medium text-gray-700">属性 {index + 1}</span>
                            <button
                              type="button"
                              onClick={() => handleRemoveEntityType(et.id)}
                              className="p-1 text-gray-400 hover:text-red-500 transition-colors"
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          </div>

                          <div className="grid grid-cols-2 gap-2">
                            <Input
                              value={et.type}
                              onChange={(e) => handleUpdateEntityType(et.id, 'type', e.target.value)}
                              placeholder="类型标识符*"
                              className="h-9 text-sm bg-white"
                            />
                            <Input
                              value={et.name}
                              onChange={(e) => handleUpdateEntityType(et.id, 'name', e.target.value)}
                              placeholder="类型名称*"
                              className="h-9 text-sm bg-white"
                            />
                          </div>

                          <Input
                            value={et.description}
                            onChange={(e) => handleUpdateEntityType(et.id, 'description', e.target.value)}
                            placeholder="描述（用于AI提取）"
                            className="h-9 text-sm bg-white"
                          />

                          {/* 🆕 快速模板 */}
                          <div className="flex gap-1.5 flex-wrap">
                            <button
                              type="button"
                              onClick={() => applyTemplate(et.id, 'price')}
                              className="px-2 py-1 text-xs bg-white border border-gray-200 rounded hover:border-gray-400 transition-colors"
                            >
                              💰 价格
                            </button>
                            <button
                              type="button"
                              onClick={() => applyTemplate(et.id, 'stage')}
                              className="px-2 py-1 text-xs bg-white border border-gray-200 rounded hover:border-gray-400 transition-colors"
                            >
                              📋 阶段
                            </button>
                            <button
                              type="button"
                              onClick={() => applyTemplate(et.id, 'date')}
                              className="px-2 py-1 text-xs bg-white border border-gray-200 rounded hover:border-gray-400 transition-colors"
                            >
                              📅 日期
                            </button>
                            <button
                              type="button"
                              onClick={() => applyTemplate(et.id, 'status')}
                              className="px-2 py-1 text-xs bg-white border border-gray-200 rounded hover:border-gray-400 transition-colors"
                            >
                              ✅ 状态
                            </button>
                          </div>

                          {/* 🆕 值类型配置 */}
                          <div className="pt-2 border-t border-gray-200 space-y-2">
                            {/* 值类型选择 */}
                            <div className="space-y-1.5">
                              <Label className="text-xs text-gray-600">值类型</Label>
                              <Select
                                value={et.value_constraints?.type || 'none'}
                                onValueChange={(value) => {
                                  if (value === 'none') {
                                    const updated = { ...et }
                                    delete updated.value_constraints
                                    setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                  } else {
                                    const updated = {
                                      ...et,
                                      value_constraints: {
                                        type: value as any,
                                      }
                                    }
                                    setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                  }
                                }}
                              >
                                <SelectTrigger className="h-8 text-xs bg-white">
                                  <SelectValue placeholder="不指定" />
                                </SelectTrigger>
                                <SelectContent className="z-[100]">
                                  <SelectItem value="none">不指定</SelectItem>
                                  <SelectItem value="text">文本</SelectItem>
                                  <SelectItem value="int">整数</SelectItem>
                                  <SelectItem value="float">浮点数</SelectItem>
                                  <SelectItem value="datetime">日期时间</SelectItem>
                                  <SelectItem value="bool">布尔值</SelectItem>
                                  <SelectItem value="enum">枚举</SelectItem>
                                </SelectContent>
                              </Select>
                            </div>

                            {/* 枚举值管理 */}
                            {et.value_constraints?.type === 'enum' && (
                              <div className="space-y-1.5">
                                <Label className="text-xs text-gray-600">枚举值</Label>
                                <div className="flex flex-wrap gap-1 mb-1">
                                  {et.value_constraints.enum_values?.map((val, i) => (
                                    <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 text-xs bg-white border border-gray-200 rounded">
                                      {val}
                                      <button
                                        type="button"
                                        onClick={() => {
                                          const newValues = [...(et.value_constraints?.enum_values || [])]
                                          newValues.splice(i, 1)
                                          const updated = {
                                            ...et,
                                            value_constraints: {
                                              ...et.value_constraints!,
                                              enum_values: newValues.length > 0 ? newValues : undefined,
                                            }
                                          }
                                          setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                        }}
                                        className="text-gray-400 hover:text-red-500"
                                      >
                                        <X className="w-2.5 h-2.5" />
                                      </button>
                                    </span>
                                  ))}
                                </div>
                                <Input
                                  placeholder="输入后按回车添加"
                                  className="h-8 text-xs bg-white"
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') {
                                      e.preventDefault()
                                      const value = e.currentTarget.value.trim()
                                      if (value) {
                                        const currentValues = et.value_constraints?.enum_values || []
                                        if (!currentValues.includes(value)) {
                                          const updated = {
                                            ...et,
                                            value_constraints: {
                                              ...et.value_constraints!,
                                              enum_values: [...currentValues, value],
                                            }
                                          }
                                          setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                          e.currentTarget.value = ''
                                        }
                                      }
                                    }
                                  }}
                                />
                              </div>
                            )}

                            {/* 单位输入 */}
                            {['int', 'float'].includes(et.value_constraints?.type || '') && (
                              <div className="space-y-1.5">
                                <Label className="text-xs text-gray-600">单位</Label>
                                <Input
                                  value={et.value_constraints?.unit || ''}
                                  onChange={(e) => {
                                    const updated = {
                                      ...et,
                                      value_constraints: {
                                        ...et.value_constraints!,
                                        unit: e.target.value || undefined,
                                      }
                                    }
                                    setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                  }}
                                  placeholder="例如：元、kg"
                                  className="h-8 text-xs bg-white"
                                />
                              </div>
                            )}

                            {/* 默认值配置 */}
                            {et.value_constraints?.type && (
                              <div className="space-y-1.5 p-2 bg-white rounded border border-gray-200">
                                <Label className="text-xs text-gray-600">默认值</Label>
                                
                                {/* 枚举类型默认值 */}
                                {et.value_constraints.type === 'enum' && et.value_constraints.enum_values && (
                                  <Select
                                    value={et.value_constraints.default || '__none__'}
                                    onValueChange={(value) => {
                                      const updated = {
                                        ...et,
                                        value_constraints: {
                                          ...et.value_constraints!,
                                          default: value === '__none__' ? undefined : value
                                        }
                                      }
                                      setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                    }}
                                  >
                                    <SelectTrigger className="h-8 text-xs bg-white">
                                      <SelectValue placeholder="选择默认值" />
                                    </SelectTrigger>
                                    <SelectContent className="z-[100]">
                                      <SelectItem value="__none__">不设置</SelectItem>
                                      {et.value_constraints.enum_values.map(val => (
                                        <SelectItem key={val} value={val}>{val}</SelectItem>
                                      ))}
                                    </SelectContent>
                                  </Select>
                                )}

                                {/* 文本类型默认值 */}
                                {et.value_constraints.type === 'text' && (
                                  <Input
                                    value={et.value_constraints.default || ''}
                                    onChange={(e) => {
                                      const updated = {
                                        ...et,
                                        value_constraints: {
                                          ...et.value_constraints!,
                                          default: e.target.value || undefined
                                        }
                                      }
                                      setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                    }}
                                    placeholder="输入默认文本"
                                    className="h-8 text-xs bg-white"
                                  />
                                )}

                                {/* 整数/浮点数默认值 */}
                                {['int', 'float'].includes(et.value_constraints.type) && (
                                  <Input
                                    type="number"
                                    step={et.value_constraints.type === 'float' ? '0.01' : '1'}
                                    value={et.value_constraints.default || ''}
                                    onChange={(e) => {
                                      const updated = {
                                        ...et,
                                        value_constraints: {
                                          ...et.value_constraints!,
                                          default: e.target.value || undefined
                                        }
                                      }
                                      setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                    }}
                                    placeholder="输入默认值"
                                    className="h-8 text-xs bg-white"
                                  />
                                )}

                                {/* 🆕 日期时间类型默认值 */}
                                {et.value_constraints.type === 'datetime' && (
                                  <Input
                                    type="datetime-local"
                                    value={et.value_constraints.default || ''}
                                    onChange={(e) => {
                                      const updated = {
                                        ...et,
                                        value_constraints: {
                                          ...et.value_constraints!,
                                          default: e.target.value || undefined
                                        }
                                      }
                                      setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                    }}
                                    className="h-8 text-xs bg-white"
                                  />
                                )}

                                {/* 布尔类型默认值 */}
                                {et.value_constraints.type === 'bool' && (
                                  <Select
                                    value={et.value_constraints.default || '__none__'}
                                    onValueChange={(value) => {
                                      const updated = {
                                        ...et,
                                        value_constraints: {
                                          ...et.value_constraints!,
                                          default: value === '__none__' ? undefined : value
                                        }
                                      }
                                      setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                    }}
                                  >
                                    <SelectTrigger className="h-8 text-xs bg-white">
                                      <SelectValue placeholder="选择默认值" />
                                    </SelectTrigger>
                                    <SelectContent className="z-[100]">
                                      <SelectItem value="__none__">不设置</SelectItem>
                                      <SelectItem value="true">是</SelectItem>
                                      <SelectItem value="false">否</SelectItem>
                                    </SelectContent>
                                  </Select>
                                )}

                                {/* 强制模式开关 */}
                                {et.value_constraints.default && (
                                  <div className="flex items-center justify-between pt-1.5 border-t border-gray-100">
                                    <div className="flex-1">
                                      <Label className="text-xs text-gray-700">强制模式</Label>
                                      <p className="text-[10px] text-gray-500">始终使用默认值</p>
                                    </div>
                                    <Switch
                                      checked={et.value_constraints.override || false}
                                      onCheckedChange={(checked) => {
                                        const updated = {
                                          ...et,
                                          value_constraints: {
                                            ...et.value_constraints!,
                                            override: checked
                                          }
                                        }
                                        setEntityTypes(entityTypes.map(item => item.id === et.id ? updated : item))
                                      }}
                                    />
                                  </div>
                                )}
                              </div>
                            )}
                          </div>

                          <div className="grid grid-cols-2 gap-2">
                            <Input
                              type="number"
                              step="0.1"
                              value={et.weight}
                              onChange={(e) => handleUpdateEntityType(et.id, 'weight', parseFloat(e.target.value) || 1.0)}
                              placeholder="权重"
                              className="h-8 text-xs bg-white"
                            />
                            <Input
                              type="number"
                              step="0.01"
                              value={et.similarity_threshold}
                              onChange={(e) => handleUpdateEntityType(et.id, 'similarity_threshold', parseFloat(e.target.value) || 0.8)}
                              placeholder="阈值"
                              className="h-8 text-xs bg-white"
                            />
                          </div>
                        </div>
                      ))}
                    </div>
                  )}

                  {entityTypes.length > 0 && (
                    <p className="text-xs text-gray-500">
                      💡 这些属性将仅用于上传的文档，优先级高于信息源和全局属性
                    </p>
                  )}
                </div>
              </CollapsibleContent>
            </Collapsible>

            {/* 拖拽上传区域 */}
            <div
              {...getRootProps()}
              className={`
                border-2 border-dashed rounded-xl p-8 text-center cursor-pointer
                transition-all duration-300
                ${isDragActive ? 'border-emerald-500 bg-emerald-50 scale-105' : 'border-emerald-200 hover:border-emerald-300 hover:bg-emerald-50/30'}
                ${!sourceId ? 'opacity-50 cursor-not-allowed' : ''}
                ${isUploading ? 'pointer-events-none' : ''}
              `}
            >
              <input {...getInputProps()} />
              <motion.div
                animate={isDragActive ? { scale: 1.1 } : { scale: 1 }}
                transition={{ duration: 0.2 }}
              >
                <Upload className="w-10 h-10 mx-auto text-gray-400 mb-3" />
              </motion.div>

              {!sourceId ? (
                <p className="text-gray-500 text-sm font-medium">
                  请先选择信息源
                </p>
              ) : isDragActive ? (
                <p className="text-emerald-600 text-sm font-medium">
                  释放文件以上传...
                </p>
              ) : (
                <div>
                  <p className="text-gray-700 text-sm font-medium mb-1">
                    拖拽文件到这里，或点击选择文件
                  </p>
                  <p className="text-xs text-gray-500">
                    支持 .md, .txt, .pdf, .html 格式
                  </p>
                </div>
              )}
            </div>

            {/* 上传状态 */}
            <AnimatePresence>
              {isUploading && (
                <motion.div
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -10 }}
                  className="bg-emerald-50 border border-emerald-200 rounded-lg p-4"
                >
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-xs text-emerald-600 font-semibold">上传中...</p>
                    <span className="text-xs text-emerald-600">{uploadProgress}%</span>
                  </div>
                  <Progress value={uploadProgress} className="h-2" />
                </motion.div>
              )}
            </AnimatePresence>

            <AnimatePresence>
              {uploadSuccess && (
                <motion.div
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -10 }}
                  className="bg-emerald-50 border border-emerald-200 rounded-lg p-4"
                >
                  <p className="text-xs text-emerald-600 font-semibold">
                    ✓ 上传成功！文档正在处理中...
                  </p>
                </motion.div>
              )}
            </AnimatePresence>

            <AnimatePresence>
              {uploadError && (
                <motion.div
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -10 }}
                  className="bg-red-50 border border-red-200 rounded-lg p-4"
                >
                  <p className="text-xs text-red-600 font-semibold">
                    ✗ 上传失败，请重试
                  </p>
                </motion.div>
              )}
            </AnimatePresence>

            {/* 按钮 */}
            <div className="flex gap-3 pt-3">
              <motion.button
                type="button"
                onClick={onClose}
                whileHover={{ scale: 1.01 }}
                whileTap={{ scale: 0.99 }}
                className="flex-1 h-11 px-4 text-sm font-medium text-gray-700 bg-gray-100 rounded-xl hover:bg-gray-200 active:bg-gray-300 transition-all"
              >
                关闭
              </motion.button>
            </div>
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  )
}

// 辅助函数：获取值类型的显示标签
function getValueTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    int: '整数',
    float: '浮点数',
    datetime: '日期时间',
    bool: '布尔值',
    enum: '枚举',
    text: '文本',
  }
  return labels[type] || type
}
