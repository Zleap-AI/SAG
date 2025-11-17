'use client'

import { useEffect } from 'react'
import './globals.css'
import { Navbar } from '@/components/layout/Navbar'
import { Footer } from '@/components/layout/Footer'
import { Providers } from './providers'
import { Toaster } from '@/components/ui/sonner'
import { usePathname } from 'next/navigation'
import { loadSavedTheme } from '@/lib/theme'


function LayoutContent({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()

  // 加载保存的主题
  useEffect(() => {
    loadSavedTheme()
  }, [])

  return (
    <div className="h-screen bg-gray-50/50 flex flex-col">
      <Navbar />
      <main className="flex-1 w-full pb-24 overflow-hidden">
        <div
          className="h-full overflow-auto scroll-smooth"
          style={{
            maxWidth: '88rem',
            margin: '0 auto',
            padding: (pathname === '/chat' || pathname === '/search') ? '0' : '2rem 1rem',
          }}
        >
          {children}
        </div>
      </main>
      <Footer />
      <Toaster />
    </div>
  )
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="zh-CN" className="h-full">
      <body className="font-sans h-full antialiased">
        <Providers>
          <LayoutContent>{children}</LayoutContent>
        </Providers>
      </body>
    </html>
  )
}
