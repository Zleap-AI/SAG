'use client'

import { useEffect, useRef } from 'react'

/**
 * 页面进入动画 Hook
 * 为整个页面添加淡入向上动画
 */
export function usePageTransition() {
  const ref = useRef<HTMLDivElement>(null)
  
  useEffect(() => {
    if (ref.current) {
      ref.current.classList.add('animate-fade-in-up')
    }
  }, [])
  
  return ref
}

/**
 * 卡片动画 Hook
 * 为卡片组件添加延迟淡入动画
 * @param delay 延迟时间（毫秒）
 */
export function useCardAnimation(delay = 0) {
  const ref = useRef<HTMLDivElement>(null)
  
  useEffect(() => {
    if (ref.current) {
      const timer = setTimeout(() => {
        ref.current?.classList.add('animate-fade-in-up')
      }, delay)
      
      return () => clearTimeout(timer)
    }
  }, [delay])
  
  return ref
}

/**
 * 交错动画 Hook
 * 为列表项添加交错淡入效果
 * @param index 列表项索引
 * @param baseDelay 基础延迟时间（毫秒）
 */
export function useStaggerAnimation(index: number, baseDelay = 100) {
  const ref = useRef<HTMLDivElement>(null)
  
  useEffect(() => {
    if (ref.current) {
      const timer = setTimeout(() => {
        ref.current?.classList.add('animate-fade-in-up')
      }, index * baseDelay)
      
      return () => clearTimeout(timer)
    }
  }, [index, baseDelay])
  
  return ref
}

/**
 * 滚动动画 Hook
 * 元素进入视口时触发动画
 */
export function useScrollAnimation() {
  const ref = useRef<HTMLDivElement>(null)
  
  useEffect(() => {
    const element = ref.current
    if (!element) return
    
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          element.classList.add('animate-fade-in-up')
          observer.unobserve(element)
        }
      },
      {
        threshold: 0.1,
        rootMargin: '0px 0px -100px 0px',
      }
    )
    
    observer.observe(element)
    
    return () => {
      if (element) {
        observer.unobserve(element)
      }
    }
  }, [])
  
  return ref
}

/**
 * 悬停动画 Hook
 * 为元素添加悬停效果
 */
export function useHoverAnimation(type: 'scale' | 'lift' | 'glow' = 'scale') {
  const ref = useRef<HTMLDivElement>(null)
  
  useEffect(() => {
    const element = ref.current
    if (!element) return
    
    const handleMouseEnter = () => {
      if (type === 'scale') {
        element.style.transform = 'scale(1.05)'
      } else if (type === 'lift') {
        element.style.transform = 'translateY(-4px)'
      } else if (type === 'glow') {
        element.style.boxShadow = '0 0 20px hsl(var(--primary) / 0.3)'
      }
    }
    
    const handleMouseLeave = () => {
      element.style.transform = ''
      element.style.boxShadow = ''
    }
    
    element.style.transition = 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)'
    element.addEventListener('mouseenter', handleMouseEnter)
    element.addEventListener('mouseleave', handleMouseLeave)
    
    return () => {
      element.removeEventListener('mouseenter', handleMouseEnter)
      element.removeEventListener('mouseleave', handleMouseLeave)
    }
  }, [type])
  
  return ref
}

