# SAG Web UI

基于 Next.js 14 的前端应用，为 SAG API 提供可视化界面。

## 🚀 快速开始

### 开发模式

```bash
# 安装依赖
npm install

# 启动开发服务器
npm run dev

# 访问应用
open http://localhost:3000
```

### 生产构建

```bash
# 构建应用
npm run build

# 启动生产服务器
npm start
```

## 📁 项目结构

```
web/
├── app/                    # Next.js App Router
│   ├── page.tsx            # 首页
│   ├── sources/            # 信息源管理
│   ├── documents/          # 文档管理
│   ├── search/             # 智能搜索
│   ├── tasks/              # 任务监控
│   └── settings/           # 系统设置
├── components/             # React 组件
│   ├── ui/                 # 基础 UI 组件
│   ├── layout/             # 布局组件
│   └── ...                 # 功能组件
├── lib/                    # 工具函数
│   ├── api-client.ts       # API 客户端
│   └── utils.ts            # 通用工具
├── store/                  # 状态管理
├── types/                  # TypeScript 类型定义
└── public/                 # 静态资源
```

## 🎨 技术栈

- **Framework**: Next.js 14 (App Router)
- **Language**: TypeScript
- **Styling**: Tailwind CSS
- **State Management**: Zustand
- **Data Fetching**: TanStack Query (React Query)
- **HTTP Client**: Axios
- **Icons**: Lucide React
- **File Upload**: React Dropzone

## 🔧 环境变量

创建 `.env.local` 文件：

```bash
# API Configuration
NEXT_PUBLIC_API_URL=http://localhost:8000
```

## 🪟 Windows 用户注意事项

如果在 Windows 上遇到路径解析问题，请按以下步骤操作：

### 1. 清理缓存并重新安装
```bash
# 删除 node_modules 和缓存
rm -rf node_modules .next
# 或者 Windows 用户使用：
# rmdir /s node_modules .next

# 重新安装依赖
npm install
```

### 2. 如果仍有路径问题，尝试使用相对路径
如果 `@/` 路径别名在 Windows 上不工作，可以临时使用相对路径：

```typescript
// 将这样的导入：
import { cn } from '@/lib/utils'

// 改为：
import { cn } from '../lib/utils'
```

### 3. 确保 Node.js 版本兼容
推荐使用 Node.js 18+ 版本：
```bash
node --version  # 应该显示 18.x 或更高版本
```

## 📖 主要功能

### 1. 信息源管理 (`/sources`)
- 创建、查看、编辑、删除信息源
- 查看信息源统计

### 2. 文档管理 (`/documents`)
- 上传文档（单个/批量）
- 查看文档列表
- 文档状态监控

### 3. 智能搜索 (`/search`)
- 支持 LLM / RAG / SAG 三种搜索模式
- 实时搜索结果展示

### 4. 任务监控 (`/tasks`)
- 实时任务状态
- 进度追踪
- 结果查看

### 5. 系统设置 (`/settings`)
- 默认实体类型配置
- API 连接设置

## 🐳 Docker 部署

### 构建镜像

```bash
docker build -t sag-web .
```

### 运行容器

```bash
docker run -p 3000:3000 \
  -e NEXT_PUBLIC_API_URL=http://localhost:8000 \
  sag-web
```

## 🔗 与后端集成

确保后端 API 服务已启动：

```bash
# 在项目根目录
python -m sag.api.main
```

API 文档: http://localhost:8000/api/docs

## 📝 开发指南

### 添加新页面

1. 在 `app/` 目录创建新文件夹
2. 创建 `page.tsx` 文件
3. 在 `components/layout/Navbar.tsx` 添加导航链接

### 添加 API 调用

在 `lib/api-client.ts` 中添加新的 API 方法：

```typescript
async getExample() {
  return this.client.get('/example')
}
```

### 添加状态管理

在 `store/` 目录创建新的 store：

```typescript
import { create } from 'zustand'

export const useExampleStore = create((set) => ({
  // state and actions
}))
```

## 🎯 性能优化

- 使用 React Query 进行数据缓存
- 图片优化（Next.js Image 组件）
- 代码分割（动态导入）
- 服务端渲染（SSR）

## 🐛 调试

```bash
# 查看构建分析
npm run build
```

## 📄 License

MIT

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！
