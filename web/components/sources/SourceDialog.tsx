'use client'

import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X } from 'lucide-react'
import { Source } from '@/types'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'

interface SourceDialogProps {
  open: boolean
  onClose: () => void
  onSubmit: (data: SourceFormData) => void
  source?: Source | null
  isLoading?: boolean
}

export interface SourceFormData {
  name: string
  description: string
}

export function SourceDialog({
  open,
  onClose,
  onSubmit,
  source,
  isLoading = false,
}: SourceDialogProps) {
  const isEditMode = !!source

  const [formData, setFormData] = useState<SourceFormData>({
    name: '',
    description: '',
  })

  const [errors, setErrors] = useState<Partial<Record<keyof SourceFormData, string>>>({})

  // 重置或填充表单
  useEffect(() => {
    if (open) {
      if (source) {
        setFormData({
          name: source.name,
          description: source.description || '',
        })
      } else {
        setFormData({
          name: '',
          description: '',
        })
      }
      setErrors({})
    }
  }, [open, source])

  const validateForm = (): boolean => {
    const newErrors: Partial<Record<keyof SourceFormData, string>> = {}

    if (!formData.name.trim()) {
      newErrors.name = '请输入信息源名称'
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

  const handleChange = (field: keyof SourceFormData, value: string) => {
    setFormData((prev) => ({ ...prev, [field]: value }))
    // 清除该字段的错误
    if (errors[field]) {
      setErrors((prev) => {
        const newErrors = { ...prev }
        delete newErrors[field]
        return newErrors
      })
    }
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
              {isEditMode ? '编辑信息源' : '创建信息源'}
            </h2>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors"
            >
              <X className="w-5 h-5 text-gray-500" />
            </button>
          </div>

          {/* 表单 */}
          <form onSubmit={handleSubmit} className="px-6 py-5 space-y-5">
            {/* 名称 */}
            <div className="space-y-2.5">
              <Label htmlFor="name-input" className="text-sm font-medium text-gray-800">
                名称 <span className="text-red-500">*</span>
              </Label>
              <Input
                id="name-input"
                type="text"
                value={formData.name}
                onChange={(e) => handleChange('name', e.target.value)}
                placeholder="例如: 我的知识库"
                className={`h-11 rounded-lg border-2 transition-all ${
                  errors.name
                    ? 'border-red-300 focus:border-red-500'
                    : 'border-gray-200 hover:border-gray-300 focus:border-gray-500'
                }`}
              />
              {errors.name && <p className="text-xs text-red-500 mt-1.5 animate-fade-in-down">{errors.name}</p>}
            </div>

            {/* 描述 */}
            <div className="space-y-2.5">
              <Label htmlFor="description-input" className="text-sm font-medium text-gray-800">
                描述
              </Label>
              <Textarea
                id="description-input"
                value={formData.description}
                onChange={(e) => handleChange('description', e.target.value)}
                placeholder="描述这个信息源的用途"
                rows={3}
                className="resize-none rounded-lg border-2 border-gray-200 hover:border-gray-300 focus:border-gray-500 transition-all"
              />
            </div>

            {/* 按钮 */}
            <div className="flex gap-3 pt-3">
              <motion.button
                type="button"
                onClick={onClose}
                whileHover={{ scale: 1.01 }}
                whileTap={{ scale: 0.99 }}
                className="flex-1 h-11 px-4 text-sm font-medium text-gray-700 bg-gray-100 rounded-xl hover:bg-gray-200 active:bg-gray-300 transition-all"
              >
                取消
              </motion.button>
              <motion.button
                type="submit"
                disabled={isLoading}
                whileHover={{ scale: isLoading ? 1 : 1.01 }}
                whileTap={{ scale: isLoading ? 1 : 0.99 }}
                className="flex-1 h-11 px-4 text-sm font-medium text-emerald-600 bg-emerald-50 rounded-xl hover:bg-emerald-100 active:bg-emerald-200 disabled:opacity-50 disabled:cursor-not-allowed shadow-sm hover:shadow transition-all"
              >
                {isLoading ? '提交中...' : isEditMode ? '更新' : '创建'}
              </motion.button>
            </div>
          </form>
        </motion.div>
      </div>
    </AnimatePresence>
  )
}
