'use client'

import { Globe, Database, FileText } from 'lucide-react'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

interface Source {
  id: string
  name: string
}

interface Article {
  id: string
  title: string
  source_config_id: string
}

interface ScopeTreeSelectorProps {
  sources: Source[]
  articles: Article[]
  value: string  // 当前选中的值：'global' | source_config_id | `article:${article_id}`
  onChange: (value: string, scope: 'global' | 'source' | 'article', sourceId?: string, articleId?: string) => void
}

export function ScopeTreeSelector({ sources, articles, value, onChange }: ScopeTreeSelectorProps) {
  const handleValueChange = (newValue: string) => {
    if (newValue === 'global') {
      onChange(newValue, 'global', undefined, undefined)
    } else if (newValue.startsWith('article:')) {
      const articleId = newValue.replace('article:', '')
      const article = articles.find(a => a.id === articleId)
      onChange(newValue, 'article', article?.source_config_id, articleId)
    } else {
      onChange(newValue, 'source', newValue, undefined)
    }
  }

  // 获取显示文本
  const getDisplayText = () => {
    if (value === 'global') {
      return (
        <div className="flex items-center gap-2">
          <Globe className="w-4 h-4 text-blue-600" />
          <span>通用</span>
        </div>
      )
    } else if (value.startsWith('article:')) {
      const articleId = value.replace('article:', '')
      const article = articles.find(a => a.id === articleId)
      return (
        <div className="flex items-center gap-2">
          <FileText className="w-4 h-4 text-blue-500" />
          <span>{article?.title || '未知文档'}</span>
        </div>
      )
    } else {
      const source = sources.find(s => s.id === value)
      return (
        <div className="flex items-center gap-2">
          <Database className="w-4 h-4 text-emerald-600" />
          <span>{source?.name || '未知信息源'}</span>
        </div>
      )
    }
  }

  return (
    <div className="space-y-2.5">
      <Label>
        应用范围 <span className="text-red-500">*</span>
      </Label>

      <Select value={value} onValueChange={handleValueChange}>
        <SelectTrigger className="h-11">
          <SelectValue>
            {getDisplayText()}
          </SelectValue>
        </SelectTrigger>

        <SelectContent className="max-h-[400px]">
          {/* 通用选项 */}
          <SelectItem value="global">
            <div className="flex items-center gap-2">
              <Globe className="w-4 h-4 text-blue-600" />
              <span>通用</span>
            </div>
          </SelectItem>

          {/* 分隔线 */}
          {sources.length > 0 && (
            <div className="h-px bg-gray-200 my-1" />
          )}

          {/* 按信息源分组 */}
          {sources.map(source => {
            const sourceArticles = articles.filter(a => a.source_config_id === source.id)

            return (
              <SelectGroup key={source.id}>
                {/* 信息源选项（不重复标题，用说明文字区分） */}
                <SelectItem value={source.id}>
                  <div className="flex items-center gap-2">
                    <Database className="w-4 h-4 text-emerald-600" />
                    <span className="font-medium">{source.name}</span>
                    <span className="text-xs text-gray-400 ml-1">(所有文档)</span>
                  </div>
                </SelectItem>

                {/* 文档列表 - 添加左边距实现视觉缩进 */}
                {sourceArticles.map(article => (
                  <SelectItem key={article.id} value={`article:${article.id}`}>
                    <div className="flex items-center gap-2 pl-4">
                      <FileText className="w-4 h-4 text-blue-500" />
                      <span className="text-sm">{article.title}</span>
                    </div>
                  </SelectItem>
                ))}
              </SelectGroup>
            )
          })}
        </SelectContent>
      </Select>

      {/* 选择预览 */}
      {value && value !== 'global' && (
        <div className="text-xs text-amber-700 px-2 py-1.5 bg-amber-50 rounded-md border border-amber-200">
          ✓ 已选择：
          {value.startsWith('article:') ? (
            (() => {
              const articleId = value.replace('article:', '')
              const article = articles.find(a => a.id === articleId)
              const source = sources.find(s => s.id === article?.source_config_id)
              return ` ${source?.name} > ${article?.title}（仅此文档）`
            })()
          ) : (
            ` ${sources.find(s => s.id === value)?.name}（该源下所有文档）`
          )}
        </div>
      )}
      {value === 'global' && (
        <div className="text-xs text-amber-700 px-2 py-1.5 bg-amber-50 rounded-md border border-amber-200">
          ✓ 已选择：通用（所有信息源和文档都可用）
        </div>
      )}
    </div>
  )
}
