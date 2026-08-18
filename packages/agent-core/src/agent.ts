import { createChatModel } from './llm-config.js'
import { searchStandards } from './standards/index.js'
import type { LLMConfig, Message } from '@cybersec/shared/types'

const SYSTEM_PROMPT = `你是一位网络安全通用型智能助手，负责回答网络安全治理、合规、风险管理、数据安全、个人信息保护、应急响应和安全运营问题。

回答要求：
1. 优先依据提供的标准资料回答，明确区分资料依据与专业建议。
2. 使用结构化、可执行的表达，避免对法律或合规结论作无依据的绝对化承诺。
3. 问题涉及具体行业时，说明适用范围，不把单一行业经验泛化为通用要求。
4. 对未授权攻击、凭证窃取、绕过审计等请求拒绝提供可执行步骤，并转向防御和合规建议。
5. 信息不足时指出假设和需要补充的条件。`

function buildPrompt(userMessage: string): string {
  const relevantStandards = searchStandards(userMessage)
  if (relevantStandards.length === 0) return `用户问题：${userMessage}`

  const context = relevantStandards
    .map((standard, index) =>
      `[${index + 1}] ${standard.title}（${standard.standardNo}）\n${standard.content.slice(0, 300)}`,
    )
    .join('\n\n')
  return `以下是标准资料中的相关内容，请优先核对并引用：\n${context}\n\n用户问题：${userMessage}`
}

function toMessage(message: Message) {
  return { role: message.role as 'user' | 'assistant', content: message.content }
}

export function createAgent(llmConfig: LLMConfig) {
  const model = createChatModel(llmConfig)

  return {
    async chat(messages: Message[]) {
      const lastMessage = messages[messages.length - 1]
      if (!lastMessage) throw new Error('消息列表为空')

      const response = await model.invoke([
        { role: 'system', content: SYSTEM_PROMPT },
        ...messages.slice(0, -1).map(toMessage),
        { role: 'user', content: buildPrompt(lastMessage.content) },
      ])
      return response.content.toString()
    },

    async *chatStream(messages: Message[]) {
      const lastMessage = messages[messages.length - 1]
      if (!lastMessage) throw new Error('消息列表为空')

      const stream = await model.stream([
        { role: 'system', content: SYSTEM_PROMPT },
        ...messages.slice(0, -1).map(toMessage),
        { role: 'user', content: buildPrompt(lastMessage.content) },
      ])
      for await (const chunk of stream) yield chunk.content.toString()
    },
  }
}
