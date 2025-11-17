'use client'

import { useState, useRef, useEffect } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { Database, LayoutGrid, Search, Settings, Activity, MessageSquare, ChevronUp, ChevronDown } from 'lucide-react'
import { motion, AnimatePresence, useMotionValue, useSpring, useTransform } from 'framer-motion'

const dockItems = [
  {
    name: '首页',
    href: '/',
    icon: LayoutGrid,
    bgGradient: 'from-orange-100/40 via-purple-100/40 to-blue-100/40',
    bgGradientHover: 'from-orange-200/70 via-purple-200/70 to-blue-200/70',
    bgGradientActive: 'from-orange-300 via-purple-300 to-blue-300',
    iconColor: 'text-purple-500',
    iconColorHover: 'text-purple-600',
    iconColorActive: 'text-purple-700',
    indicatorColor: 'bg-gradient-to-r from-orange-500 to-purple-500',
  },
  {
    name: '智能搜索',
    href: '/search',
    icon: Search,
    bgGradient: 'from-blue-100/40 to-indigo-100/40',
    bgGradientHover: 'from-blue-200/70 to-indigo-200/70',
    bgGradientActive: 'from-blue-300 to-indigo-300',
    iconColor: 'text-blue-400',
    iconColorHover: 'text-blue-500',
    iconColorActive: 'text-blue-600',
    indicatorColor: 'bg-blue-500',
  },
  {
    name: 'AI问答',
    href: '/chat',
    icon: MessageSquare,
    bgGradient: 'from-purple-100/40 to-pink-100/40',
    bgGradientHover: 'from-purple-200/70 to-pink-200/70',
    bgGradientActive: 'from-purple-300 to-pink-300',
    iconColor: 'text-purple-400',
    iconColorHover: 'text-purple-500',
    iconColorActive: 'text-purple-600',
    indicatorColor: 'bg-purple-500',
  },
  { type: 'divider' as const },
  {
    name: '信息源',
    href: '/sources',
    icon: Database,
    bgGradient: 'from-emerald-100/40 to-teal-100/40',
    bgGradientHover: 'from-emerald-200/70 to-teal-200/70',
    bgGradientActive: 'from-emerald-300 to-teal-300',
    iconColor: 'text-emerald-400',
    iconColorHover: 'text-emerald-500',
    iconColorActive: 'text-emerald-600',
    indicatorColor: 'bg-emerald-500',
  },
  {
    name: '任务监控',
    href: '/tasks',
    icon: Activity,
    bgGradient: 'from-pink-100/40 to-rose-100/40',
    bgGradientHover: 'from-pink-200/70 to-rose-200/70',
    bgGradientActive: 'from-pink-300 to-rose-300',
    iconColor: 'text-pink-400',
    iconColorHover: 'text-pink-500',
    iconColorActive: 'text-pink-600',
    indicatorColor: 'bg-pink-500',
  },
  {
    name: '设置',
    href: '/settings',
    icon: Settings,
    bgGradient: 'from-gray-100/40 to-slate-100/40',
    bgGradientHover: 'from-gray-200/70 to-slate-200/70',
    bgGradientActive: 'from-gray-300 to-slate-300',
    iconColor: 'text-gray-400',
    iconColorHover: 'text-gray-500',
    iconColorActive: 'text-gray-600',
    indicatorColor: 'bg-gray-500',
  },
]

interface DockIconProps {
  item: typeof dockItems[number]
  mouseX: ReturnType<typeof useMotionValue<number>>
  isActive: boolean
}

function DockIcon({ item, mouseX, isActive }: DockIconProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [isHovered, setIsHovered] = useState(false)

  const distance = useMotionValue(100)
  const widthSync = useTransform(distance, [-150, 0, 150], [52, 68, 52])
  const width = useSpring(widthSync, { mass: 0.1, stiffness: 200, damping: 15 })

  useEffect(() => {
    const iconElement = ref.current
    if (!iconElement) return

    const updateDistance = () => {
      const rect = iconElement.getBoundingClientRect()
      const iconCenterX = rect.left + rect.width / 2
      const mouseDist = mouseX.get() - iconCenterX
      distance.set(mouseDist)
    }

    const unsubscribe = mouseX.on('change', updateDistance)
    return unsubscribe
  }, [mouseX, distance])

  if (item.type === 'divider') {
    return (
      <div className="w-px h-12 bg-gray-300/50 mx-2" />
    )
  }

  const Icon = item.icon!

  // 根据状态选择颜色
  const bgGradient = isActive
    ? item.bgGradientActive
    : isHovered
    ? item.bgGradientHover
    : item.bgGradient

  const iconColor = isActive
    ? item.iconColorActive
    : isHovered
    ? item.iconColorHover
    : item.iconColor

  return (
    <Link href={item.href!}>
      <motion.div
        ref={ref}
        style={{ width }}
        className="flex flex-col items-center justify-end group relative"
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
      >
        {/* 活跃指示器 */}
        {isActive && (
          <motion.div
            layoutId="activeIndicator"
            className={`absolute -bottom-1 w-1.5 h-1.5 ${item.indicatorColor} rounded-full shadow-lg`}
            transition={{ type: "spring", stiffness: 380, damping: 30 }}
          />
        )}

        {/* 悬停指示器 */}
        {!isActive && isHovered && (
          <motion.div
            initial={{ opacity: 0, scale: 0.5 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.5 }}
            className={`absolute -bottom-1 w-1 h-1 ${item.indicatorColor} opacity-40 rounded-full`}
          />
        )}

        {/* 图标容器 */}
        <motion.div
          className={`w-full aspect-square rounded-2xl bg-linear-to-br ${bgGradient}
                     backdrop-blur-xl flex items-center justify-center
                     shadow-lg shadow-black/10 transition-all duration-300
                     hover:shadow-xl hover:shadow-black/20
                     relative overflow-hidden`}
        >
          <Icon className={`w-[55%] h-[55%] ${iconColor} transition-colors duration-300`} strokeWidth={1.8} />
        </motion.div>

        {/* 名称标签 - 悬停时显示 */}
        <AnimatePresence>
          {isHovered && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: -8 }}
              exit={{ opacity: 0, y: 10 }}
              transition={{ duration: 0.2 }}
              className="absolute -top-8 bg-gray-800/90 text-white text-xs px-2 py-1 rounded backdrop-blur-sm pointer-events-none whitespace-nowrap"
            >
              {item.name}
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </Link>
  )
}

export function Footer() {
  const pathname = usePathname()

  // 初始状态固定为 true，避免 hydration 错误
  const [isExpanded, setIsExpanded] = useState(true)
  const [mounted, setMounted] = useState(false)

  const mouseX = useMotionValue(0)

  // 客户端挂载后读取 localStorage
  useEffect(() => {
    setMounted(true)
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('dock-expanded')
      if (saved !== null) {
        setIsExpanded(JSON.parse(saved))
      }
    }
  }, [])

  // 保存用户偏好到 localStorage
  useEffect(() => {
    if (mounted && typeof window !== 'undefined') {
      localStorage.setItem('dock-expanded', JSON.stringify(isExpanded))
    }
  }, [isExpanded, mounted])

  const toggleExpanded = () => {
    setIsExpanded(!isExpanded)
  }

  // 根据当前路径获取触发器颜色配置
  const getTriggerColors = () => {
    // 检查是否在设置相关页面（包括子页面）
    if (pathname.startsWith('/settings')) {
      return {
        bg: 'bg-gray-50/60',
        bgHover: 'hover:bg-gray-100/80',
        border: 'border-gray-200/30',
        icon: 'text-gray-400',
        indicator: 'bg-gray-200/50'
      }
    }
    
    // 检查是否在信息源相关页面（包括文档管理）
    if (pathname === '/sources' || pathname.startsWith('/documents')) {
      return {
        bg: 'bg-emerald-50/60',
        bgHover: 'hover:bg-emerald-100/80',
        border: 'border-emerald-200/30',
        icon: 'text-emerald-400',
        indicator: 'bg-emerald-200/50'
      }
    }
    
    switch (pathname) {
      case '/':
        return {
          bg: 'bg-gradient-to-br from-orange-50/60 via-purple-50/60 to-blue-50/60',
          bgHover: 'hover:from-orange-100/80 hover:via-purple-100/80 hover:to-blue-100/80',
          border: 'border-purple-200/30',
          icon: 'text-purple-400',
          indicator: 'bg-purple-200/50'
        }
      case '/search':
        return {
          bg: 'bg-blue-50/60',
          bgHover: 'hover:bg-blue-100/80',
          border: 'border-blue-200/30',
          icon: 'text-blue-400',
          indicator: 'bg-blue-200/50'
        }
      case '/chat':
        return {
          bg: 'bg-purple-50/60',
          bgHover: 'hover:bg-purple-100/80',
          border: 'border-purple-200/30',
          icon: 'text-purple-400',
          indicator: 'bg-purple-200/50'
        }
      case '/tasks':
        return {
          bg: 'bg-pink-50/60',
          bgHover: 'hover:bg-pink-100/80',
          border: 'border-pink-200/30',
          icon: 'text-pink-400',
          indicator: 'bg-pink-200/50'
        }
      default:
        return {
          bg: 'bg-purple-50/60',
          bgHover: 'hover:bg-purple-100/80',
          border: 'border-purple-200/30',
          icon: 'text-purple-400',
          indicator: 'bg-purple-200/50'
        }
    }
  }

  const triggerColors = getTriggerColors()

  return (
    <motion.footer className="fixed bottom-0 left-0 right-0 z-[60]">
      <AnimatePresence mode="wait">
        {isExpanded ? (
          <motion.div
            key="expanded"
            initial={{ y: 100, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 100, opacity: 0 }}
            transition={{
              duration: 0.5,
              ease: [0.32, 0.72, 0, 1],
            }}
            className="pb-4"
            onMouseMove={(e) => mouseX.set(e.pageX)}
            onMouseLeave={() => mouseX.set(Infinity)}
          >
            {/* 收起按钮 */}
            <div className="flex justify-center mb-2">
              <motion.button
                onClick={toggleExpanded}
                className={`flex items-center justify-center w-10 h-6 rounded-full backdrop-blur-md
                          transition-colors cursor-pointer shadow-lg ${triggerColors.bg} ${triggerColors.bgHover} ${triggerColors.border}`}
                whileHover={{ scale: 1.1 }}
                whileTap={{ scale: 0.95 }}
              >
                <ChevronDown className={`w-4 h-4 ${triggerColors.icon}`} />
              </motion.button>
            </div>

            {/* macOS Dock */}
            <div className="flex justify-center px-4">
              <motion.div
                className="flex items-end gap-2 px-3 py-3 bg-gradient-to-t from-gray-50/30 to-white/50 backdrop-blur-2xl rounded-2xl
                          border border-gray-200/40 shadow-2xl shadow-gray-200/20"
                initial={{ scale: 0.9 }}
                animate={{ scale: 1 }}
                transition={{ type: "spring", stiffness: 260, damping: 20 }}
              >
                {dockItems.map((item, index) => {
                  // 判断是否激活：需要匹配子路径的页面
                  let isActive = false
                  if (item.type !== 'divider') {
                    if (item.href === '/settings') {
                      // 设置页面：匹配 /settings/*
                      isActive = pathname.startsWith('/settings')
                    } else if (item.href === '/sources') {
                      // 信息源页面：匹配 /sources 和 /documents
                      isActive = pathname === '/sources' || pathname.startsWith('/documents')
                    } else {
                      // 其他页面：严格匹配
                      isActive = pathname === item.href
                    }
                  }
                  
                  return (
                  <DockIcon
                    key={item.type === 'divider' ? `divider-${index}` : item.href}
                    item={item}
                    mouseX={mouseX}
                      isActive={isActive}
                  />
                  )
                })}
              </motion.div>
            </div>
          </motion.div>
        ) : (
          <motion.div
            key="collapsed"
            initial={{ y: 100, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 100, opacity: 0 }}
            transition={{
              duration: 0.3,
              ease: [0.32, 0.72, 0, 1],
            }}
            className="flex items-center justify-center py-3"
          >
            {/* 展开按钮 */}
            <motion.button
              onClick={toggleExpanded}
              className="flex flex-col items-center gap-1.5 px-8 py-2 cursor-pointer group"
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.95 }}
            >
              <motion.div
                animate={{
                  y: [0, -3, 0],
                }}
                transition={{
                  duration: 2,
                  repeat: Infinity,
                  ease: "easeInOut",
                }}
                className={`w-12 h-12 rounded-full backdrop-blur-md shadow-lg
                          flex items-center justify-center transition-colors
                          ${triggerColors.bg} ${triggerColors.bgHover} ${triggerColors.border}`}
              >
                <ChevronUp className={`w-5 h-5 ${triggerColors.icon}`} />
              </motion.div>
              <div className={`w-8 h-1 rounded-full ${triggerColors.indicator}`} />
            </motion.button>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.footer>
  )
}

