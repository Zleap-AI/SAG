'use client'

import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Tag } from 'lucide-react'
import { Document } from '@/types'
import { Badge } from '@/components/ui/badge'

interface DocumentEditDialogProps {
  open: boolean
  onClose: () => void
  onSubmit: (data: DocumentFormData) => void
  document?: Document | null
  isLoading?: boolean
}

export interface DocumentFormData {
  title: string
  summary: string
  category: string
  tags: string[]
}

export function DocumentEditDialog({
  open,
  onClose,
  onSubmit,
  document,
  isLoading = false,
}: DocumentEditDialogProps) {
  const isEditMode = !!document

  const [formData, setFormData] = useState<DocumentFormData>({
    title: '',
    summary: '',
    category: '',
    tags: [],
  })

  const [errors, setErrors] = useState<Partial<Record<keyof DocumentFormData, string>>>({})
  const [tagInput, setTagInput] = useState('')

  // 重置或填充表单
  useEffect(() => {
    if (open) {
      if (document) {
        setFormData({
          title: document.title,
          summary: document.summary || '',
          category: document.category || '',
          tags: document.tags || [],
        })
      } else {
        setFormData({
          title: '',
          summary: '',
          category: '',
          tags: [],
        })
      }
      setTagInput('')
      setErrors({})
    }
  }, [open, document])

  const validateForm = (): boolean => {
    const newErrors: Partial<Record<keyof DocumentFormData, string>> = {}

    if (!formData.title.trim()) {
      newErrors.title = '请输入文档标题'
    }

    setErrors(newErrors)
    return Object.keys(newErrors).length === 0
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (validateForm()) {
      onSubmit(formData)
    }
  }

  const handleChange = (field: keyof Omit<DocumentFormData, 'tags'>, value: string) => {
    setFormData((prev) => ({ ...prev, [field]: value }))
    // 清除该字段的错误
    if (errors[field]) {
      setErrors((prev) => ({ ...prev, [field]: '' }))
    }
  }

  const handleAddTag = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && tagInput.trim()) {
      e.preventDefault()
      const newTag = tagInput.trim()
      if (!formData.tags.includes(newTag)) {
        setFormData((prev) => ({
          ...prev,
          tags: [...prev.tags, newTag],
        }))
      }
      setTagInput('')
    }
  }

  const handleRemoveTag = (tagToRemove: string) => {
    setFormData((prev) => ({
      ...prev,
      tags: prev.tags.filter((tag) => tag !== tagToRemove),
    }))
  }

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
              {isEditMode ? '编辑文档' : '编辑文档'}
            </h2>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors"
            >
              <X className="w-5 h-5 text-gray-500" />
            </button>
          </div>

          {/* 表单 */}
          <form onSubmit={handleSubmit} className="px-6 py-5 space-y-5 max-h-[calc(100vh-200px)] overflow-y-auto scrollbar-hide">
            {/* 标题 */}
            <div className="space-y-2.5">
              <label htmlFor="title" className="text-sm font-medium text-gray-800">
                标题 <span className="text-red-500">*</span>
              </label>
              <input
                id="title"
                type="text"
                value={formData.title}
                onChange={(e) => handleChange('title', e.target.value)}
                placeholder="输入文档标题"
                disabled={isLoading}
                className={`w-full h-11 px-3 rounded-lg border-2 transition-all ${
                  errors.title 
                    ? 'border-red-300 focus:border-red-500' 
                    : 'border-gray-200 hover:border-gray-300 focus:border-gray-500'
                } ${isLoading ? 'bg-gray-50 cursor-not-allowed opacity-60' : ''}`}
              />
              {errors.title && (
                <p className="text-xs text-red-500 mt-1.5 animate-fade-in-down">{errors.title}</p>
              )}
            </div>

            {/* 摘要 */}
            <div className="space-y-2.5">
              <label htmlFor="summary" className="text-sm font-medium text-gray-800">
                摘要
              </label>
              <textarea
                id="summary"
                value={formData.summary}
                onChange={(e) => handleChange('summary', e.target.value)}
                placeholder="输入文档摘要（可选）"
                rows={3}
                disabled={isLoading}
                className="w-full px-3 py-2 resize-none rounded-lg border-2 border-gray-200 hover:border-gray-300 focus:border-gray-500 transition-all"
              />
            </div>

            {/* 分类 */}
            <div className="space-y-2.5">
              <label htmlFor="category" className="text-sm font-medium text-gray-800">
                分类
              </label>
              <input
                id="category"
                type="text"
                value={formData.category}
                onChange={(e) => handleChange('category', e.target.value)}
                placeholder="输入文档分类（可选）"
                disabled={isLoading}
                className="w-full h-11 px-3 rounded-lg border-2 border-gray-200 hover:border-gray-300 focus:border-gray-500 transition-all"
              />
            </div>

            {/* 标签 */}
            <div className="space-y-2.5">
              <label htmlFor="tags" className="text-sm font-medium text-gray-800">
                标签
              </label>
              <input
                id="tags"
                type="text"
                value={tagInput}
                onChange={(e) => setTagInput(e.target.value)}
                onKeyDown={handleAddTag}
                placeholder="输入标签后按回车添加"
                disabled={isLoading}
                className="w-full h-11 px-3 rounded-lg border-2 border-gray-200 hover:border-gray-300 focus:border-gray-500 transition-all"
              />
              {formData.tags.length > 0 && (
                <div className="flex flex-wrap gap-2 mt-2">
                  {formData.tags.map((tag) => (
                    <Badge
                      key={tag}
                      variant="outline"
                      className="flex items-center gap-1 px-2 py-1 bg-emerald-50 text-emerald-700 border-emerald-200"
                    >
                      {tag}
                      <button
                        type="button"
                        onClick={() => handleRemoveTag(tag)}
                        className="ml-1 hover:text-emerald-900"
                        disabled={isLoading}
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </Badge>
                  ))}
                </div>
              )}
            </div>

            {/* 按钮 */}
            <div className="flex gap-3 pt-3">
              <motion.button
                type="button"
                onClick={onClose}
                whileHover={{ scale: 1.01 }}
                whileTap={{ scale: 0.99 }}
                className="flex-1 h-11 px-4 text-sm font-medium text-gray-700 bg-gray-100 rounded-xl hover:bg-gray-200 active:bg-gray-300 transition-all"
                disabled={isLoading}
              >
                取消
              </motion.button>
              <motion.button
                type="submit"
                disabled={isLoading}
                whileHover={{ scale: isLoading ? 1 : 1.01 }}
                whileTap={{ scale: isLoading ? 1 : 0.99 }}
                className="flex-1 h-11 px-4 text-sm font-medium text-emerald-700 bg-emerald-50 rounded-xl hover:bg-emerald-100 active:bg-emerald-200 disabled:opacity-50 disabled:cursor-not-allowed shadow-sm transition-all"
              >
                {isLoading ? '保存中...' : '保存'}
              </motion.button>
            </div>
          </form>
        </motion.div>
      </div>
    </AnimatePresence>
  )
}