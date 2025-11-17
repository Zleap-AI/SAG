'use client'

import Link from 'next/link'
import { Database, Search, Code, MessageSquare, Activity, Settings } from 'lucide-react'
import { motion } from 'framer-motion'

const features = [
  {
    icon: Search,
    title: '智能搜索',
    href: '/search',
    bgGradient: 'from-blue-100 to-indigo-100',
    iconColor: 'text-blue-600',
  },
  {
    icon: MessageSquare,
    title: 'AI问答',
    href: '/chat',
    bgGradient: 'from-purple-100 to-pink-100',
    iconColor: 'text-purple-600',
  },
  {
    icon: Database,
    title: '信息源管理',
    href: '/sources',
    bgGradient: 'from-emerald-100 to-teal-100',
    iconColor: 'text-emerald-600',
  },
  {
    icon: Activity,
    title: '任务监控',
    href: '/tasks',
    bgGradient: 'from-pink-100 to-rose-100',
    iconColor: 'text-pink-600',
  },
  {
    icon: Settings,
    title: '系统设置',
    href: '/settings',
    bgGradient: 'from-gray-100 to-slate-100',
    iconColor: 'text-gray-600',
  },
  {
    icon: Code,
    title: 'API 文档',
    href: process.env.NEXT_PUBLIC_API_URL + '/api/docs' || 'http://localhost:8000/api/docs',
    external: true,
    bgGradient: 'from-indigo-100 to-blue-100',
    iconColor: 'text-indigo-600',
  },
]

function FeatureCard({ feature, index }: { feature: typeof features[0]; index: number }) {
  const Icon = feature.icon

  return (
    <Link href={feature.href} target={feature.external ? '_blank' : undefined}>
      <motion.div
        initial={{ opacity: 0, scale: 0.8 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.4, delay: index * 0.05 }}
        className="flex flex-col items-center justify-center group"
      >
        {/* 图标容器 - iOS毛玻璃效果 */}
        <div className={`
          bg-linear-to-br ${feature.bgGradient}
          w-24 h-24 rounded-[22%]
          flex items-center justify-center
          backdrop-blur-xl bg-opacity-70
          shadow-lg shadow-black/10
          transition-all duration-300
          group-hover:scale-110 group-hover:shadow-xl group-hover:shadow-black/20
          cursor-pointer
          relative
          before:absolute before:inset-0 before:rounded-[22%] before:bg-white/20 before:opacity-0 group-hover:before:opacity-100 before:transition-opacity
        `}>
          <Icon className={`w-13 h-13 ${feature.iconColor}`} strokeWidth={1.8} />
        </div>

        {/* 标题 */}
        <p className="text-xs font-medium text-gray-700 mt-2 text-center">
          {feature.title}
        </p>
      </motion.div>
    </Link>
  )
}

export default function Home() {
  return (
    <div className="absolute inset-0 overflow-auto">
      {/* 极简浅色背景 */}
      <div className="fixed inset-0 bg-white -z-10" />

      {/* 垂直居中容器 */}
      <div className="min-h-full flex items-center justify-center px-8">
        {/* iOS风格图标网格 - 居中 */}
        <div className="grid grid-cols-3 gap-x-20 gap-y-14 max-w-2xl">
          {features.map((feature, index) => (
            <FeatureCard key={feature.title} feature={feature} index={index} />
          ))}
        </div>
      </div>
    </div>
  )
}
