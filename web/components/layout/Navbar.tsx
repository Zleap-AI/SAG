'use client'

import Link from 'next/link'
import { Zap, Github, Heart, ExternalLink, MoreVertical } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { motion } from 'framer-motion'

export function Navbar() {
  return (
    <nav className="bg-white/40 backdrop-blur-2xl border-b border-orange-200/20 sticky top-0 z-50 shadow-sm shadow-orange-100/20">
      <div className="mx-auto px-6">
        <div className="flex justify-between h-14">
          {/* Logo区域 */}
          <div className="flex items-center space-x-3.5">
            <Link
              href="/"
              className="flex items-center space-x-3 group"
            >
              {/* Logo图标 - iOS 风格多彩渐变 */}
              <div className="relative p-2 rounded-xl bg-gradient-to-br from-pink-400/15 via-purple-400/15 via-blue-400/15 to-cyan-400/15
                            shadow-lg shadow-purple-200/20 group-hover:shadow-xl group-hover:shadow-blue-200/30
                            transition-all duration-300">
                <div className="absolute inset-0 rounded-xl bg-gradient-to-tr from-white/50 to-transparent opacity-60" />
                <div className="absolute inset-0 rounded-xl bg-gradient-to-bl from-transparent via-transparent to-purple-300/10" />
                <motion.div
                  animate={{
                    rotate: [0, 90, 180, 270, 360],
                  }}
                  transition={{
                    duration: 6,
                    repeat: Infinity,
                    ease: "easeInOut",
                    times: [0, 0.15, 0.5, 0.85, 1],
                  }}
                  style={{
                    transformOrigin: 'center',
                  }}
                  className="relative z-10"
                >
                  <Zap className="w-5 h-5 text-yellow-500 drop-shadow-sm" />
                </motion.div>
              </div>

              <div className="flex flex-col">
                {/* 标题行 */}
                <div className="flex items-center gap-2">
                  <h1 className="text-md ">
                    SAG
                  </h1>
                  {/* 版本Badge - 精致毛玻璃效果 */}
                  <Badge variant="secondary" className="hidden sm:inline-flex text-[10px] font-medium
                                                        bg-linear-to-br from-gray-100/80 to-gray-200/60
                                                        backdrop-blur-sm text-gray-500 border-0 h-5 px-2.5
                                                        shadow-sm">
                    Alpha
                  </Badge>
                </div>
                {/* Slogan - 流光动画效果 */}
                <p
                  className="text-[11px] leading-none italic tracking-wide font-light shimmer-text"
                  data-text="Data in motion, Thought in action"
                >
                  Data in motion, Thought in action
                </p>
              </div>
            </Link>
          </div>

          {/* 右侧菜单 - hover 展开 */}
          <div className="flex items-center">
            <div className="relative group">
              {/* 三点按钮 */}
              <button
                className="p-2 rounded-lg text-gray-500 hover:text-gray-800
                          hover:bg-gray-100/80 transition-all duration-200
                          focus:outline-none"
                aria-label="更多选项"
              >
                <MoreVertical className="w-4 h-4" />
              </button>

              {/* hover 展开的菜单 */}
              <div className="absolute right-0 top-full mt-2 w-48 bg-white/95 backdrop-blur-xl
                            border border-gray-200/50 shadow-xl rounded-xl p-1
                            opacity-0 invisible group-hover:opacity-100 group-hover:visible
                            transition-all duration-200 ease-out
                            group-hover:translate-y-0 -translate-y-2">
                <a
                  href="https://github.com/zleap-team/sag"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-3 px-3 py-2.5 text-sm text-gray-700
                            hover:bg-gray-100/80 rounded-lg transition-colors group/item"
                >
                  <Github className="w-4 h-4" />
                  <span className="font-medium">GitHub</span>
                </a>
                <a
                  href="http://localhost:8000/api/docs"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-3 px-3 py-2.5 text-sm text-gray-700
                            hover:bg-gray-100/80 rounded-lg transition-colors group/item"
                >
                  <ExternalLink className="w-4 h-4" />
                  <span className="font-medium">API 文档</span>
                </a>
                <a
                  href="https://github.com/zleap-team"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-3 px-3 py-2.5 text-sm text-gray-700
                            hover:bg-gray-100/80 rounded-lg transition-colors group/item"
                >
                  <Heart className="w-4 h-4 text-red-400 fill-red-400" />
                  <span className="font-medium">技术支持</span>
                </a>
              </div>
            </div>
          </div>
        </div>
      </div>
    </nav>
  )
}

