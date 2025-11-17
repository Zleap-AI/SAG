import axios, { AxiosInstance, AxiosRequestConfig } from 'axios'

// API 基础路径配置
// 开发环境: 使用 NEXT_PUBLIC_API_URL 环境变量（如 http://localhost:8000）直接访问后端
// 生产环境/Docker: 使用空字符串（相对路径），通过 Nginx 代理到后端 API
const getApiBaseUrl = () => {
  // 如果明确设置了 NEXT_PUBLIC_API_URL，使用它（本地开发）
  if (process.env.NEXT_PUBLIC_API_URL) {
    return process.env.NEXT_PUBLIC_API_URL
  }
  
  // 否则使用相对路径（Docker/生产环境，走Nginx代理）
  return ''
}

const API_BASE_URL = getApiBaseUrl()

class ApiClient {
  private client: AxiosInstance

  constructor() {
    this.client = axios.create({
      baseURL: `${API_BASE_URL}/api/v1`,
      timeout: 3600000, // 1小时超时，适应文档处理等长时间操作
      headers: {
        'Content-Type': 'application/json',
      },
    })

    // 请求拦截器
    this.client.interceptors.request.use(
      (config) => {
        // 可以在这里添加认证 token
        // const token = localStorage.getItem('token')
        // if (token) {
        //   config.headers.Authorization = `Bearer ${token}`
        // }
        return config
      },
      (error) => Promise.reject(error)
    )

    // 响应拦截器
    this.client.interceptors.response.use(
      (response) => response.data,
      (error) => {
        const message = error.response?.data?.error?.message || error.message
        console.error('API Error:', message)
        return Promise.reject(error)
      }
    )
  }

  // 信息源管理
  async getSources(params?: { page?: number; page_size?: number; name?: string }) {
    return this.client.get('/sources', { params })
  }

  async getSource(id: string) {
    return this.client.get(`/sources/${id}`)
  }

  async createSource(data: { name: string; description?: string; config?: any }) {
    return this.client.post('/sources', data)
  }

  async updateSource(id: string, data: any) {
    return this.client.patch(`/sources/${id}`, data)
  }

  async deleteSource(id: string) {
    return this.client.delete(`/sources/${id}`)
  }

  // 实体类型管理
  async getDefaultEntityTypes() {
    return this.client.get('/entity-types/defaults')
  }

  async getEntityTypes(sourceId: string, params?: any) {
    return this.client.get(`/sources/${sourceId}/entity-types`, { params })
  }

  async createEntityType(sourceId: string, data: any) {
    return this.client.post(`/sources/${sourceId}/entity-types`, data)
  }

  async updateEntityType(id: string, data: any) {
    return this.client.patch(`/entity-types/${id}`, data)
  }

  async deleteEntityType(id: string) {
    return this.client.delete(`/entity-types/${id}`)
  }

  // 全局自定义实体类型管理
  async getGlobalEntityTypes(params?: { page?: number; page_size?: number; only_active?: boolean }) {
    return this.client.get('/entity-types', { params })
  }

  async createGlobalEntityType(data: {
    type: string
    name: string
    description?: string
    weight?: number
    similarity_threshold?: number
    extraction_prompt?: string
    extraction_examples?: any[]
  }) {
    return this.client.post('/entity-types', data)
  }

  // 获取所有实体类型（系统默认 + 全局 + 所有信息源专属）
  async getAllEntityTypes(params?: { page?: number; page_size?: number; only_active?: boolean }) {
    return this.client.get('/entity-types/all', { params })
  }

  // 🆕 文档级别实体类型管理
  async createArticleEntityType(articleId: string, data: {
    type: string
    name: string
    description?: string
    weight?: number
    similarity_threshold?: number
    extraction_prompt?: string
    extraction_examples?: any[]
    value_format?: string
    value_constraints?: any
  }) {
    return this.client.post(`/documents/${articleId}/entity-types`, data)
  }

  async getArticleEntityTypes(articleId: string, params?: {
    page?: number
    page_size?: number
    only_active?: boolean
  }) {
    return this.client.get(`/documents/${articleId}/entity-types`, { params })
  }

  // 🆕 获取所有文档（跨信息源）
  async getArticles(params?: {
    page?: number
    page_size?: number
    status?: string
    source_config_id?: string  // 可选：按信息源筛选
  }) {
    return this.client.get('/documents', { params })
  }

  // 文档管理
  async uploadDocument(
    sourceId: string, 
    file: File, 
    autoProcess = true, 
    background?: string,
    entityTypes?: any[]  // 🆕 文档专属实体类型配置
  ) {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('auto_process', String(autoProcess))
    if (background) {
      formData.append('background', background)
    }
    // 🆕 添加实体类型配置
    if (entityTypes && entityTypes.length > 0) {
      formData.append('entity_types', JSON.stringify(entityTypes))
    }

    return this.client.post(`/sources/${sourceId}/documents/upload`, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
  }

  async uploadMultipleDocuments(sourceId: string, files: File[], autoProcess = true) {
    const formData = new FormData()
    files.forEach(file => formData.append('files', file))
    formData.append('auto_process', String(autoProcess))

    return this.client.post(`/sources/${sourceId}/documents/upload-multiple`, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
  }

  async getDocuments(sourceId: string, params?: any) {
    return this.client.get(`/sources/${sourceId}/documents`, { params })
  }

  async getDocument(id: string) {
    return this.client.get(`/documents/${id}`)
  }

  async deleteDocument(id: string) {
    return this.client.delete(`/documents/${id}`)
  }

  async updateDocument(id: string, data: any) {
    return this.client.put(`/documents/${id}`, data)
  }

  async getDocumentSections(articleId: string) {
    return this.client.get(`/documents/${articleId}/sections`)
  }

  async getDocumentEvents(articleId: string) {
    return this.client.get(`/documents/${articleId}/events`)
  }

  // 流程执行
  async runPipeline(data: any) {
    return this.client.post('/pipeline/run', data)
  }

  async runPipelineSync(data: any) {
    return this.client.post('/pipeline/run-sync', data)
  }

  async runLoad(data: any) {
    return this.client.post('/pipeline/load', data)
  }

  async runExtract(data: any) {
    return this.client.post('/pipeline/extract', data)
  }

  async runSearch(data: any) {
    return this.client.post('/pipeline/search', data)
  }

  // 任务管理
  async getTasks(params?: any) {
    return this.client.get('/tasks', { params })
  }

  async getTask(id: string) {
    return this.client.get(`/tasks/${id}`)
  }

  async getTasksStats() {
    return this.client.get('/tasks/stats')
  }

  async batchDeleteTasks(data: { task_ids?: string[]; status_filter?: string[] }) {
    return this.client.delete('/tasks/batch', { data })
  }

  async cancelTask(id: string) {
    return this.client.post(`/tasks/${id}/cancel`)
  }

  // 原始请求方法
  async get(url: string, config?: AxiosRequestConfig) {
    return this.client.get(url, config)
  }

  async post(url: string, data?: any, config?: AxiosRequestConfig) {
    return this.client.post(url, data, config)
  }

  async put(url: string, data?: any, config?: AxiosRequestConfig) {
    return this.client.put(url, data, config)
  }

  async patch(url: string, data?: any, config?: AxiosRequestConfig) {
    return this.client.patch(url, data, config)
  }

  async delete(url: string, config?: AxiosRequestConfig) {
    return this.client.delete(url, config)
  }

  // 🆕 AI 对话相关
  async *chatStream(data: {
    query: string
    source_config_ids: string[]
    mode: 'quick' | 'deep'
    context?: any[]
    params?: any
  }): AsyncGenerator<any> {
    const apiUrl = `${API_BASE_URL}/api/v1/chat/message`
    
    const response = await fetch(apiUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(data),
    })

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`)
    }

    const reader = response.body?.getReader()
    if (!reader) throw new Error('No response body')

    const decoder = new TextDecoder()
    let buffer = ''

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6))
              yield data
            } catch (e) {
              console.error('Failed to parse SSE data:', line, e)
            }
          }
        }
      }
    } finally {
      reader.releaseLock()
    }
  }

  async submitChatFeedback(data: {
    message_id: string
    rating: number
    feedback_type: string
    comment?: string
  }) {
    return this.client.post('/chat/feedback', data)
  }

  // 模型配置管理（LLM、Embedding等）
  async getModelConfigs(params?: { type?: string; scenario?: string; is_active?: boolean }) {
    return this.client.get('/model-configs', { params })
  }

  async getModelConfig(id: string) {
    return this.client.get(`/model-configs/${id}`)
  }

  async createModelConfig(data: {
    name: string
    description?: string
    type?: 'llm' | 'embedding' | 'rerank'
    scenario?: string
    provider?: string
    api_key: string
    base_url: string
    model: string
    temperature?: number
    max_tokens?: number
    top_p?: number
    frequency_penalty?: number
    presence_penalty?: number
    timeout?: number
    max_retries?: number
    extra_data?: { dimensions?: number; [key: string]: any }
    is_active?: boolean
    priority?: number
  }) {
    return this.client.post('/model-configs', data)
  }

  async updateModelConfig(id: string, data: any) {
    return this.client.put(`/model-configs/${id}`, data)
  }

  async deleteModelConfig(id: string) {
    return this.client.delete(`/model-configs/${id}`)
  }

  async testModelConfig(id: string, test_message?: string) {
    return this.client.post(`/model-configs/${id}/test`, { test_message })
  }
}

export const apiClient = new ApiClient()
export default apiClient

