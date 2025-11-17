'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { BookOpen, ChevronDown } from 'lucide-react'
import { apiClient } from '@/lib/api-client'
import { ArticleSection } from '@/types'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Badge } from '@/components/ui/badge'
import { formatDate } from '@/lib/utils'

interface SectionDrawerProps {
  open: boolean
  onClose: () => void
  articleId?: string
}

export function SectionDrawer({ open, onClose, articleId }: SectionDrawerProps) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())

  const { data: sectionsData, isLoading } = useQuery({
    queryKey: ['documentSections', articleId],
    queryFn: () => articleId ? apiClient.getDocumentSections(articleId) : null,
    enabled: !!articleId && open,
  })

  const sections = (sectionsData?.data || []) as ArticleSection[]

  const toggleExpand = (id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  return (
    <Sheet open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <SheetContent
        side="right"
        className="w-full sm:w-[650px] sm:max-w-[650px]"
        overlayClassName="bg-black/20"
      >
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <BookOpen className="w-5 h-5 text-emerald-600" />
            文章片段
          </SheetTitle>
          <SheetDescription>
            共 {sections.length} 个片段
          </SheetDescription>
        </SheetHeader>

        <div className="mt-6 space-y-4 overflow-y-auto" style={{ maxHeight: 'calc(100vh - 140px)' }}>
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <div className="text-sm text-gray-500">加载中...</div>
            </div>
          ) : sections.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <BookOpen className="w-12 h-12 text-emerald-200 mb-3" />
              <p className="text-sm text-gray-500">暂无片段</p>
            </div>
          ) : (
            sections.map((section, index) => {
              const isExpanded = expandedIds.has(section.id)
              const contentLength = section.content.length
              const shouldShowToggle = contentLength > 500

              return (
                <motion.div
                  key={section.id}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.2, delay: index * 0.05 }}
                >
                  <button
                    onClick={() => shouldShowToggle && toggleExpand(section.id)}
                    className="w-full text-left relative border-0 rounded-lg p-5 bg-white/80 backdrop-blur-sm shadow-md hover:shadow-lg transition-all duration-300"
                  >
                    {/* 排序号 */}
                    <div className="absolute top-4 right-4">
                      <Badge variant="outline" className="text-xs bg-emerald-50 text-emerald-700 border-emerald-200">
                        #{section.rank + 1}
                      </Badge>
                    </div>

                    {/* 标题 */}
                    <h3 className="text-base font-semibold text-gray-900 mb-3 pr-12">
                      {section.heading || `片段 ${index + 1}`}
                    </h3>

                    {/* 内容 */}
                    <div className="relative">
                      <motion.p
                        initial={false}
                        animate={{ height: isExpanded ? 'auto' : 'fit-content' }}
                        transition={{ duration: 0.3, ease: 'easeInOut' }}
                        className={`text-sm text-gray-600 mb-4 whitespace-pre-wrap ${isExpanded ? '' : 'line-clamp-6'
                          }`}
                      >
                        {section.content}
                      </motion.p>

                      {/* 折叠时的渐变遮罩 + 展开提示 */}
                      {!isExpanded && shouldShowToggle && (
                        <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-white via-white to-transparent flex items-end justify-center pb-4">
                          <span className="text-xs text-emerald-600 flex items-center gap-1 font-medium">
                            点击展开 <ChevronDown className="w-3 h-3" />
                          </span>
                        </div>
                      )}
                    </div>

                    {/* 元数据 */}
                    <div className="flex items-center justify-between text-xs text-gray-500 pt-3 border-t border-gray-100 mt-2">
                      <span>创建时间: {formatDate(section.created_time)}</span>
                      {section.extra_data?.type && (
                        <Badge variant="outline" className="text-xs bg-gray-50">
                          {section.extra_data.type}
                        </Badge>
                      )}
                    </div>
                  </button>
                </motion.div>
              )
            })
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
