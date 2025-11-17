'use client'

import { useState } from 'react'
import { motion } from 'framer-motion'
import { Tag, Edit2, Trash2, MoreVertical, Database, FileText, Globe } from 'lucide-react'
import { EntityType } from '@/types'
import { Switch } from '@/components/ui/switch'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

interface EntityTypeCardProps {
  entityType: EntityType
  sourceName?: string
  articleTitle?: string  // 🆕 文档标题
  onEdit?: (entityType: EntityType) => void
  onDelete?: (entityType: EntityType) => void
  onToggle?: (entityType: EntityType, isActive: boolean) => void
  index?: number
}

export function EntityTypeCard({
  entityType,
  sourceName,
  articleTitle,
  onEdit,
  onDelete,
  onToggle,
  index = 0,
}: EntityTypeCardProps) {
  const [isToggling, setIsToggling] = useState(false)
  
  // 判断类型
  const isDefault = entityType.is_default
  const isArticle = entityType.scope === 'article' || entityType.article_id
  const isSource = (entityType.scope === 'source' || entityType.source_config_id) && !isArticle
  const isGlobal = entityType.scope === 'global' || (!entityType.source_config_id && !entityType.is_default && !isArticle)

  // 🎨 根据类型获取图标和颜色
  const getIconStyle = () => {
    if (isArticle) return 'bg-blue-50'      // 文档：浅蓝
    if (isSource) return 'bg-emerald-50'    // 信息源：浅绿
    if (isGlobal) return 'bg-yellow-50'       // 全局：浅蓝
    return 'bg-yellow-50'                   // 系统默认：浅黄
  }

  const getIconColor = () => {
    if (isArticle) return 'text-blue-600'     // 文档：蓝色
    if (isSource) return 'text-emerald-600'   // 信息源：绿色
    if (isGlobal) return 'text-yellow-600'      // 全局：蓝色
    return 'text-yellow-600'                  // 系统默认：黄色
  }

  const getIcon = () => {
    if (isArticle) return FileText   // 📄 文档
    if (isSource) return Database    // 📚 信息源
    if (isGlobal) return Globe       // 🌍 全局
    return Tag                       // 🏷️ 默认
  }

  const Icon = getIcon()

  const handleToggle = async (checked: boolean) => {
    if (isDefault) return // 系统默认属性不允许切换
    setIsToggling(true)
    try {
      await onToggle?.(entityType, checked)
    } finally {
      setIsToggling(false)
    }
  }

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
        {/* 开关（自定义类型才显示） */}
        {!isDefault && (
          <Switch
            checked={entityType.is_active}
            onCheckedChange={handleToggle}
            disabled={isToggling}
            className="data-[state=checked]:bg-yellow-500"
          />
        )}

        {/* 更多操作菜单（自定义类型才显示） */}
        {!isDefault && (
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
                onClick={() => onEdit?.(entityType)}
                className="cursor-pointer text-sm"
              >
                <Edit2 className="w-4 h-4 mr-2" />
                编辑
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => onDelete?.(entityType)}
                className="cursor-pointer text-sm text-red-600 focus:text-red-600"
              >
                <Trash2 className="w-4 h-4 mr-2" />
                删除
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>

      {/* 垂直布局：图标 + 内容 */}
      <div className="space-y-4">
        {/* 顶部：图标 + 属性名称 */}
        <div className="flex items-start gap-3 mb-2">
          <div className={`p-1.5 rounded-lg shrink-0 ${getIconStyle()}`}>
            <Icon className={`w-4 h-4 ${getIconColor()}`} />
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="font-semibold text-lg text-gray-900 truncate">
              {entityType.name}
            </h3>
            <p className="text-xs text-gray-400">({entityType.type})</p>
          </div>
        </div>

        {/* 描述 */}
        {entityType.description && (
          <p className="text-sm text-gray-600 line-clamp-2">
            {entityType.description}
          </p>
        )}

        {/* 参数信息 */}
        <div className="space-y-2 text-sm">
          <div className="flex items-center justify-between">
            <span className="text-gray-500">权重:</span>
            <span className="font-medium text-gray-700">{entityType.weight}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-gray-500">阈值:</span>
            <span className="font-medium text-gray-700">{entityType.similarity_threshold}</span>
          </div>

          {/* 🆕 显示值类型 */}
          {entityType.value_constraints?.type && (
            <div className="flex items-center justify-between">
              <span className="text-gray-500">值类型:</span>
              <span className="font-medium text-gray-700">
                {getValueTypeLabel(entityType.value_constraints.type)}
              </span>
            </div>
          )}

          {/* 🆕 显示枚举值 */}
          {entityType.value_constraints?.type === 'enum' &&
           entityType.value_constraints.enum_values && (
            <div className="pt-2 border-t border-gray-200">
              <span className="text-xs text-gray-500">可选值:</span>
              <div className="flex flex-wrap gap-1 mt-1">
                {entityType.value_constraints.enum_values.slice(0, 3).map((val, i) => (
                  <span
                    key={i}
                    className="px-2 py-0.5 text-xs bg-gray-100 text-gray-700 rounded"
                  >
                    {val}
                  </span>
                ))}
                {entityType.value_constraints.enum_values.length > 3 && (
                  <span className="px-2 py-0.5 text-xs text-gray-500">
                    +{entityType.value_constraints.enum_values.length - 3}
                  </span>
                )}
              </div>
            </div>
          )}

          {/* 🆕 显示数值范围 */}
          {['int', 'float'].includes(entityType.value_constraints?.type || '') && (
            <div className="pt-2 border-t border-gray-200 space-y-1">
              {(entityType.value_constraints?.min !== undefined ||
                entityType.value_constraints?.max !== undefined) && (
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-500">范围:</span>
                  <span className="text-gray-700">
                    {entityType.value_constraints.min ?? '不限'} ~ {entityType.value_constraints.max ?? '不限'}
                  </span>
                </div>
              )}
              {entityType.value_constraints?.unit && (
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-500">单位:</span>
                  <span className="text-gray-700">{entityType.value_constraints.unit}</span>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </motion.div>
  )
}

// 🆕 辅助函数：获取值类型的显示标签
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

