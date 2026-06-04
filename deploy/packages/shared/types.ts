export interface LLMConfig {
  baseUrl: string
  apiKey: string
  model: string
  provider: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: number
}

export interface ChatRequest {
  messages: Message[]
  llmConfig: LLMConfig
}

export interface StandardEntry {
  id: string
  category: string
  title: string
  standardNo: string
  content: string
  tags: string[]
}

export const DEFAULT_PROVIDERS = [
  {
    name: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com',
    models: ['deepseek-chat', 'deepseek-coder'],
  },
  {
    name: 'ModelScope',
    baseUrl: 'https://api.modelscope.cn',
    models: ['qwen-max', 'qwen-plus', 'qwen-turbo'],
  },
  {
    name: '硅基流动',
    baseUrl: 'https://api.siliconflow.cn',
    models: ['Qwen/Qwen2.5-7B-Instruct', 'deepseek-ai/DeepSeek-V3'],
  },
]