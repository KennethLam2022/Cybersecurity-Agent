import { ChatOpenAI } from '@langchain/openai'
import type { LLMConfig } from '@cybersec/shared/types.js'

export function createChatModel(config: LLMConfig) {
  if (!config.baseUrl || !config.apiKey || !config.model) {
    throw new Error('LLM 配置不完整: 需要 baseUrl、apiKey 和 model')
  }

  return new ChatOpenAI({
    model: config.model,
    apiKey: config.apiKey,
    configuration: {
      baseURL: config.baseUrl,
    },
    temperature: 0.3,
    maxRetries: 2,
  })
}