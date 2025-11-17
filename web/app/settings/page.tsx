'use client'

import Link from 'next/link'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { 
  Settings, 
  Box,
  Sparkles as SparklesIcon,
  ChevronRight,
  Layers
} from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { apiClient } from '@/lib/api-client'

export default function SettingsPage() {
  // 获取统计数据
  const { data: entityTypesData } = useQuery({
    queryKey: ['allEntityTypes'],
    queryFn: () => apiClient.getAllEntityTypes({ page: 1, page_size: 1000 }),
  })

  const { data: modelConfigsData } = useQuery({
    queryKey: ['model-configs-stats'],
    queryFn: () => apiClient.getModelConfigs({ type: 'llm' })
  })

  const entityTypesCount = entityTypesData?.data?.length || 0
  const modelConfigsCount = modelConfigsData?.data?.length || 0

  const settings = [
    {
      id: 'entity',
      title: '实体维度',
      description: '管理实体类型定义和属性配置',
      icon: Box,
      href: '/settings/entity',
      iconBg: 'bg-gradient-to-br from-amber-100 to-yellow-100',
      iconColor: 'text-amber-600',
      hoverColor: 'hover:border-amber-200',
      stats: `${entityTypesCount} 个维度`,
      badge: null
    },
    {
      id: 'model',
      title: 'LLM模型',
      description: '配置大语言模型的场景化参数',
      icon: SparklesIcon,
      href: '/settings/model',
      iconBg: 'bg-gradient-to-br from-indigo-100 to-purple-100',
      iconColor: 'text-indigo-600',
      hoverColor: 'hover:border-indigo-200',
      stats: `${modelConfigsCount} 个配置`,
      badge: 'NEW'
    },
  ]

  return (
    <div className="space-y-8 pb-8">
      {/* 页面标题 */}
          <motion.div 
            className="flex items-center gap-3"
        initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
          >
        <div className="p-2.5 rounded-xl bg-gradient-to-br from-gray-100 to-gray-200 shadow-md">
          <Settings className="w-6 h-6 text-gray-500" />
            </div>
            <div>
          <h1 className="text-xl font-bold text-gray-800">系统设置</h1>
          <p className="text-gray-500 text-xs">配置和管理系统参数</p>
        </div>
      </motion.div>

      {/* 设置卡片 */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {settings.map((setting, index) => {
            const Icon = setting.icon

            return (
              <Link key={setting.id} href={setting.href}>
                <motion.div
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: index * 0.1 }}
                whileHover={{ y: -4 }}
                >
                <Card className={`group relative overflow-hidden border-gray-200 ${setting.hoverColor} hover:shadow-xl transition-all duration-300 cursor-pointer`}>
                  <CardContent className="p-6">
                    <div className="flex items-start justify-between">
                        <div className="flex items-center gap-4 flex-1">
                          {/* Icon */}
                        <div className={`p-3 rounded-xl ${setting.iconBg} shadow-md group-hover:scale-110 transition-transform duration-300`}>
                          <Icon className={`w-6 h-6 ${setting.iconColor}`} />
                          </div>
                          
                        {/* 标题和描述 */}
                          <div className="flex-1">
                            <div className="flex items-center gap-2 mb-1">
                              <h3 className="text-lg font-semibold text-gray-900">
                                {setting.title}
                              </h3>
                              {setting.badge && (
                              <Badge className="bg-gradient-to-r from-indigo-400 to-purple-400 text-white border-0 text-xs">
                                <SparklesIcon className="w-3 h-3 mr-1" />
                                  {setting.badge}
                                </Badge>
                              )}
                            </div>
                          <p className="text-sm text-gray-500 mb-3">
                              {setting.description}
                            </p>
                          
                          {/* 统计信息 */}
                          <div className="flex items-center gap-2">
                            <Layers className="w-4 h-4 text-gray-400" />
                            <span className="text-sm font-medium text-gray-600">
                              {setting.stats}
                            </span>
                          </div>
                        </div>
                      </div>

                      {/* 箭头 */}
                      <ChevronRight className={`w-5 h-5 text-gray-400 group-hover:${setting.iconColor} group-hover:translate-x-1 transition-all duration-300 flex-shrink-0`} />
                      </div>
                    </CardContent>
                  </Card>
                </motion.div>
              </Link>
            )
          })}
      </div>
    </div>
  )
}

