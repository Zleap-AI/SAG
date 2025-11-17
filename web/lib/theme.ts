// 主题配置中心 - 快速修改主色调
export const theme = {
  colors: {
    primary: {
      DEFAULT: 'hsl(262 83% 58%)',     // 紫色
      hover: 'hsl(262 83% 50%)',
      light: 'hsl(262 80% 95%)',
    },
    secondary: {
      DEFAULT: 'hsl(220 14.3% 95.9%)',
      hover: 'hsl(220 14.3% 90%)',
    },
  },
  animation: {
    duration: {
      fast: '0.2s',
      normal: '0.3s',
      slow: '0.5s',
    },
    easing: {
      default: 'cubic-bezier(0.4, 0, 0.2, 1)',
      smooth: 'cubic-bezier(0.16, 1, 0.3, 1)',
    },
  },
  spacing: {
    cardPadding: '1.5rem',
    sectionGap: '5rem',
  },
  borderRadius: {
    sm: '0.375rem',
    md: '0.5rem',
    lg: '0.75rem',
  },
}

// 预设主题色 - 快速切换
export const themePresets = {
  purple: {
    name: '紫色',
    primary: '262 83% 58%',
    accent: '262 80% 95%',
    gradient: 'from-purple-600 via-blue-600 to-indigo-600',
  },
  blue: {
    name: '蓝色',
    primary: '221 83% 53%',
    accent: '221 80% 95%',
    gradient: 'from-blue-600 via-cyan-600 to-teal-600',
  },
  green: {
    name: '绿色',
    primary: '142 71% 45%',
    accent: '142 70% 95%',
    gradient: 'from-green-600 via-emerald-600 to-teal-600',
  },
  orange: {
    name: '橙色',
    primary: '25 95% 53%',
    accent: '25 90% 95%',
    gradient: 'from-orange-600 via-amber-600 to-yellow-600',
  },
  pink: {
    name: '粉色',
    primary: '330 81% 60%',
    accent: '330 80% 95%',
    gradient: 'from-pink-600 via-rose-600 to-red-600',
  },
}

// 获取当前主题
export function getCurrentTheme() {
  return themePresets.purple // 默认紫色主题
}

// 应用主题（可在设置中使用）
export function applyTheme(themeKey: keyof typeof themePresets) {
  const theme = themePresets[themeKey]
  if (theme) {
    document.documentElement.style.setProperty('--primary', theme.primary)
    document.documentElement.style.setProperty('--accent', theme.accent)
    localStorage.setItem('theme-color', themeKey)
  }
}

// 加载保存的主题
export function loadSavedTheme() {
  if (typeof window !== 'undefined') {
    const savedTheme = localStorage.getItem('theme-color') as keyof typeof themePresets
    if (savedTheme && themePresets[savedTheme]) {
      applyTheme(savedTheme)
    }
  }
}

