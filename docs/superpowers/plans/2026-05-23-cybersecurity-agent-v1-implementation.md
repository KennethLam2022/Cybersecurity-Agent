# 网络安全移动运营商智能 Agent V1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建第一版基于 LangChain.js 的移动运营商网络安全标准合规问答 Agent，含美观的对话界面

**Architecture:** Monorepo（pnpm workspace），包含 `agent-core`（LangChain Agent 核心逻辑）、`web-app`（Next.js 前端）、`shared`（共享类型）。前端全屏对话界面，浅色专业风，后端 SSE 流式输出，LLM 用户自配。

**Tech Stack:** pnpm workspace · Next.js 14 (App Router) · Tailwind CSS · shadcn/ui · Framer Motion · LangChain.js · TypeScript

---

### Task 1: 项目 Monorepo 脚手架

**Files:**
- Create: `package.json`（根）
- Create: `pnpm-workspace.yaml`
- Create: `.gitignore`
- Create: `tsconfig.base.json`

- [ ] **Step 1: 创建根 package.json**

```json
{
  "name": "cybersecurity-agent",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "pnpm --filter web-app dev",
    "build": "pnpm --filter agent-core build && pnpm --filter web-app build",
    "lint": "pnpm -r lint"
  },
  "engines": {
    "node": ">=18"
  }
}
```

- [ ] **Step 2: 创建 pnpm-workspace.yaml**

```yaml
packages:
  - "packages/*"
```

- [ ] **Step 3: 创建 .gitignore**

```
node_modules/
dist/
.next/
*.env
.env.local
```

- [ ] **Step 4: 创建 tsconfig.base.json**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true,
    "declaration": true,
    "declarationMap": true,
    "sourceMap": true
  }
}
```

- [ ] **Step 5: 初始化 git 仓库并提交**

```bash
git init
git add -A
git commit -m "chore: init monorepo scaffold"
```

---

### Task 2: 共享类型定义（shared）

**Files:**
- Create: `packages/shared/package.json`
- Create: `packages/shared/tsconfig.json`
- Create: `packages/shared/types.ts`

- [ ] **Step 1: 创建 package.json**

```json
{
  "name": "@cybersec/shared",
  "version": "1.0.0",
  "private": true,
  "main": "./types.ts",
  "types": "./types.ts"
}
```

- [ ] **Step 2: 创建 tsconfig.json**

```json
{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "outDir": "./dist"
  },
  "include": ["*.ts"]
}
```

- [ ] **Step 3: 写入 types.ts**

```typescript
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
```

- [ ] **Step 4: 提交**

```bash
git add packages/shared/
git commit -m "feat: add shared types and default LLM providers"
```

---

### Task 3: Agent Core — 包配置与入口

**Files:**
- Create: `packages/agent-core/package.json`
- Create: `packages/agent-core/tsconfig.json`
- Create: `packages/agent-core/src/index.ts`

- [ ] **Step 1: 创建 package.json**

```json
{
  "name": "@cybersec/agent-core",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "main": "./dist/index.js",
  "types": "./dist/index.d.ts",
  "scripts": {
    "build": "tsc",
    "dev": "tsc --watch"
  },
  "dependencies": {
    "@langchain/core": "^0.3.0",
    "@langchain/langgraph": "^0.2.0",
    "@langchain/openai": "^0.4.0",
    "zod": "^3.23.0"
  },
  "devDependencies": {
    "typescript": "^5.6.0"
  }
}
```

- [ ] **Step 2: 创建 tsconfig.json**

```json
{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "outDir": "./dist",
    "rootDir": "./src"
  },
  "include": ["src/**/*"]
}
```

- [ ] **Step 3: 创建入口文件 src/index.ts**

```typescript
export { createAgent } from './agent.js'
export { type LLMConfig } from '@cybersec/shared/types.js'
```

- [ ] **Step 4: 提交**

```bash
git add packages/agent-core/
git commit -m "feat: init agent-core package"
```

---

### Task 4: Agent Core — LLM 配置管理

**Files:**
- Create: `packages/agent-core/src/llm-config.ts`

- [ ] **Step 1: 创建 llm-config.ts**

```typescript
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
```

- [ ] **Step 2: 提交**

```bash
git add packages/agent-core/src/llm-config.ts
git commit -m "feat: add LLM config manager with ChatOpenAI wrapper"
```

---

### Task 5: Agent Core — 内置标准知识库

**Files:**
- Create: `packages/agent-core/src/standards/index.ts`
- Create: `packages/agent-core/src/standards/djbh-data.ts`
- Create: `packages/agent-core/src/standards/data-security-data.ts`

- [ ] **Step 1: 创建等保标准数据**

```typescript
// packages/agent-core/src/standards/djbh-data.ts
import type { StandardEntry } from '@cybersec/shared/types.js'

export const djbhStandards: StandardEntry[] = [
  {
    id: 'djbh-001',
    category: '等级保护',
    title: '网络安全等级保护基本要求（三级）',
    standardNo: 'GB/T 22239-2019',
    content: `三级系统（安全标记保护级）的核心安全要求包括：

【物理安全】机房应配备温湿度控制、UPS不间断电源、视频监控系统；机房出入应实行双人值守。
【网络安全】应划分安全区域，区域间实施访问控制；应部署入侵检测系统；应对网络设备进行安全审计。
【主机安全】应实现身份鉴别（口令+生物特征等双因素）；应遵循最小权限原则；应启用安全审计功能。
【应用安全】应实现应用层访问控制；应对通信数据进行完整性保护；应提供应用安全审计。
【数据安全】应对用户个人信息进行保护；数据传输应采用加密通道；应定期备份数据。`,
    tags: ['等保三级', '基本要求', 'GB/T 22239', '5G核心网'],
  },
  {
    id: 'djbh-002',
    category: '等级保护',
    title: '网络安全等级保护定级指南',
    standardNo: 'GB/T 22240-2020',
    content: `等级保护对象定级要素包括：
- 受侵害的客体（公民/法人权益、社会秩序/公共利益、国家安全）
- 对客体的侵害程度（一般损害、严重损害、特别严重损害）

核心网系统通常定为第三级（三级=安全标记保护级）。
BOSS/CRM等支撑系统根据业务重要性定为二级或三级。
5G核心网建议定为三级，因其一旦受损将严重影响通信秩序。`,
    tags: ['等保定级', 'GB/T 22240', '5G核心网', '定级指南'],
  },
  {
    id: 'djbh-003',
    category: '等级保护',
    title: '等保三级测评高风险判定指引',
    standardNo: 'ISEAA 001-2020',
    content: `等保三级测评中以下情形判定为高风险：

1. 物理安全：核心机房未配备电子门禁或视频监控
2. 网络安全：未在网络边界部署访问控制设备（防火墙/ACL）
3. 网络安全：核心系统未划分安全域
4. 主机安全：未实现双因素身份鉴别
5. 数据安全：未对用户敏感信息进行加密存储
6. 数据安全：未建立备份恢复机制
7. 管理安全：未建立安全管理机构和安全管理制度`,
    tags: ['高风险判定', '等保测评', 'ISEAA 001', '测评要求'],
  },
]
```

- [ ] **Step 2: 创建数据安全法数据**

```typescript
// packages/agent-core/src/standards/data-security-data.ts
import type { StandardEntry } from '@cybersec/shared/types.js'

export const dataSecurityStandards: StandardEntry[] = [
  {
    id: 'ds-001',
    category: '数据安全法',
    title: '中华人民共和国数据安全法',
    standardNo: '2021年6月10日通过',
    content: `数据安全法对运营商的核心要求：

【第三条】数据处理包括数据的收集、存储、使用、加工、传输、提供、公开等。
【第二十一条】建立数据分类分级保护制度。运营商应识别核心数据、重要数据和一般数据。
【第二十七条】开展数据处理活动应当建立健全数据安全管理制度，采取必要技术措施。
【第三十条】重要数据的处理者应明确数据安全负责人和管理机构，落实数据安全保护责任。
【第三十一条】关键信息基础设施的运营者在中国境内运营中收集和产生的重要数据应在境内存储。
【第四十五条】不履行数据安全保护义务的，可处五十万至二百万元罚款。`,
    tags: ['数据安全法', '分类分级', '重要数据', '运营商'],
  },
  {
    id: 'ds-002',
    category: '数据安全法',
    title: '基础电信企业数据分类分级方法',
    standardNo: 'YD/T 3813-2020',
    content: `基础电信企业数据分类分级框架：

【分类维度】用户数据、业务数据、运营管理数据、网络与设备数据
【分级维度】核心数据（危害国家安全/经济运行）、重要数据（危害公共利益/个人权益）、一般数据
【典型重要数据】用户身份信息（IMSI/IMEI）、位置信息、通信记录、通话清单、上网记录
【运营商要求】应建立数据资产台账，明确数据分类分级标签；不同等级数据实施差异化管控措施。`,
    tags: ['数据分类分级', 'YD/T 3813', '电信企业', '重要数据'],
  },
  {
    id: 'ds-003',
    category: '数据安全法',
    title: '个人信息保护法',
    standardNo: '2021年8月20日通过',
    content: `个人信息保护法对运营商的核心要求：

【第六条】收集个人信息应当限于实现处理目的的最小范围（最小必要原则）。
【第十三条】处理个人信息需取得个人同意（法律另有规定的除外）。
【第四十四条】个人有权查阅、复制、更正、删除其个人信息。
【第五十一条】应采取加密、去标识化等安全技术措施。
【第五十五条】涉及自动化决策、委托处理、向第三方提供个人信息等情形，应事前进行个人信息保护影响评估。
【第六十六条】违法处理个人信息情节严重的，可处五千万元以下或上一年度营业额百分之五以下罚款。`,
    tags: ['个人信息保护法', '个保法', '最小必要', '用户同意'],
  },
]
```

- [ ] **Step 3: 创建标准库索引**

```typescript
// packages/agent-core/src/standards/index.ts
import type { StandardEntry } from '@cybersec/shared/types.js'
import { djbhStandards } from './djbh-data.js'
import { dataSecurityStandards } from './data-security-data.js'

const allStandards: StandardEntry[] = [...djbhStandards, ...dataSecurityStandards]

export function searchStandards(query: string): StandardEntry[] {
  const q = query.toLowerCase()
  return allStandards.filter(
    (s) =>
      s.title.toLowerCase().includes(q) ||
      s.content.toLowerCase().includes(q) ||
      s.tags.some((t) => t.toLowerCase().includes(q)) ||
      s.standardNo.toLowerCase().includes(q)
  )
}

export function getStandardById(id: string): StandardEntry | undefined {
  return allStandards.find((s) => s.id === id)
}

export { allStandards }
```

- [ ] **Step 4: 提交**

```bash
git add packages/agent-core/src/standards/
git commit -m "feat: add built-in standard knowledge base (等保+数据安全)"
```

---

### Task 6: Agent Core — Agent 编排

**Files:**
- Create: `packages/agent-core/src/agent.ts`

- [ ] **Step 1: 创建 agent.ts**

```typescript
import { createChatModel } from './llm-config.js'
import { searchStandards } from './standards/index.js'
import type { LLMConfig, Message } from '@cybersec/shared/types.js'

const SYSTEM_PROMPT = `你是一位移动运营商网络安全管理体系专家。你的职责是回答网络安全合规类问题。

回答规则：
1. 如果用户问的是标准/合规问题，优先引用内置标准库的数据，给出标准编号和具体条款
2. 回答格式使用结构化列表，重要内容用加粗标记
3. 如果问题不在内置标准库中，基于你的专业知识回答，并说明"这是基于专业经验的回答"
4. 回答末尾标注引用来源（标准编号）
5. 回答简洁、直白，不绕弯子
6. 如果问题不明确，反问确认

你的专业领域：
- 等级保护（等保2.0/3.0）
- 关键信息基础设施安全（CII）
- 数据安全法/个人信息保护法
- 通信行业标准（YD系列）
- 网络安全体系（ISO27000/风险评估/应急响应）`

function buildPrompt(userMessage: string): string {
  const relevantStandards = searchStandards(userMessage)
  let context = ''

  if (relevantStandards.length > 0) {
    context = `\n\n以下是内置标准库中相关的条款，请优先引用这些内容：\n${relevantStandards
      .map(
        (s, i) =>
          `[${i + 1}] ${s.title}（${s.standardNo}）\n${s.content.slice(0, 300)}`
      )
      .join('\n\n')}`
  }

  return `${context}\n\n用户问题：${userMessage}`
}

export function createAgent(llmConfig: LLMConfig) {
  const model = createChatModel(llmConfig)

  return {
    async chat(messages: Message[]) {
      const lastMsg = messages[messages.length - 1]
      if (!lastMsg) throw new Error('消息列表为空')

      const prompt = buildPrompt(lastMsg.content)
      const response = await model.invoke([
        { role: 'system', content: SYSTEM_PROMPT },
        ...messages.slice(0, -1).map((m) => ({
          role: m.role as 'user' | 'assistant',
          content: m.content,
        })),
        { role: 'user', content: prompt },
      ])

      return response.content.toString()
    },

    async *chatStream(messages: Message[]) {
      const lastMsg = messages[messages.length - 1]
      if (!lastMsg) throw new Error('消息列表为空')

      const prompt = buildPrompt(lastMsg.content)
      const stream = await model.stream([
        { role: 'system', content: SYSTEM_PROMPT },
        ...messages.slice(0, -1).map((m) => ({
          role: m.role as 'user' | 'assistant',
          content: m.content,
        })),
        { role: 'user', content: prompt },
      ])

      for await (const chunk of stream) {
        yield chunk.content.toString()
      }
    },
  }
}
```

- [ ] **Step 2: 提交**

```bash
git add packages/agent-core/src/agent.ts
git commit -m "feat: implement agent orchestration with streaming"
```

---

### Task 7: Web App — Next.js 脚手架

**Files:**
- Create: `packages/web-app/package.json`
- Create: `packages/web-app/tsconfig.json`
- Create: `packages/web-app/next.config.js`
- Create: `packages/web-app/tailwind.config.ts`
- Create: `packages/web-app/postcss.config.js`
- Create: `packages/web-app/src/app/layout.tsx`
- Create: `packages/web-app/src/app/globals.css`

- [ ] **Step 1: 创建 package.json**

```json
{
  "name": "@cybersec/web-app",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "lint": "next lint"
  },
  "dependencies": {
    "next": "^14.2.0",
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "framer-motion": "^11.0.0",
    "lucide-react": "^0.400.0",
    "clsx": "^2.1.0"
  },
  "devDependencies": {
    "@types/node": "^20.0.0",
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "typescript": "^5.6.0",
    "tailwindcss": "^3.4.0",
    "postcss": "^8.4.0",
    "autoprefixer": "^10.4.0"
  }
}
```

- [ ] **Step 2: 创建 tailwind.config.ts**

```typescript
import type { Config } from 'tailwindcss'

const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#e8f0fe',
          100: '#c6dafc',
          200: '#90b4f9',
          300: '#5a8df5',
          400: '#3b7bf2',
          500: '#1a73e8',
          600: '#155cb8',
          700: '#104588',
          800: '#0b2e58',
          900: '#051728',
        },
      },
    },
  },
  plugins: [],
}

export default config
```

- [ ] **Step 3: 创建 postcss.config.js**

```javascript
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
```

- [ ] **Step 4: 创建 globals.css**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  body {
    @apply bg-white text-gray-900 antialiased;
  }
}

@layer utilities {
  .scrollbar-thin {
    scrollbar-width: thin;
    scrollbar-color: #d0d0d0 transparent;
  }
}
```

- [ ] **Step 5: 创建 layout.tsx**

```tsx
import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: '网络安全移动运营商智能 Agent',
  description: '基于知识库的网络安全合规智能问答系统',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="zh-CN">
      <body className="h-screen overflow-hidden">{children}</body>
    </html>
  )
}
```

- [ ] **Step 6: 创建 next.config.js**

```javascript
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
}

module.exports = nextConfig
```

- [ ] **Step 7: 提交**

```bash
git add packages/web-app/
git commit -m "feat: init Next.js web app scaffold"
```

---

### Task 8: Web App — API 路由（聊天接口）

**Files:**
- Create: `packages/web-app/src/app/api/chat/route.ts`

- [ ] **Step 1: 创建聊天 API 路由**

```typescript
import { NextRequest } from 'next/server'
import { createAgent } from '@cybersec/agent-core'
import type { LLMConfig, Message } from '@cybersec/shared/types'

export const runtime = 'nodejs'
export const maxDuration = 60

export async function POST(req: NextRequest) {
  try {
    const body = await req.json()
    const { messages, llmConfig } = body as {
      messages: Message[]
      llmConfig: LLMConfig
    }

    if (!messages?.length) {
      return Response.json({ error: '消息列表为空' }, { status: 400 })
    }
    if (!llmConfig?.baseUrl || !llmConfig?.apiKey || !llmConfig?.model) {
      return Response.json({ error: 'LLM 配置不完整' }, { status: 400 })
    }

    const agent = createAgent(llmConfig)

    const encoder = new TextEncoder()
    const stream = new ReadableStream({
      async start(controller) {
        try {
          for await (const chunk of agent.chatStream(messages)) {
            controller.enqueue(encoder.encode(`data: ${JSON.stringify({ content: chunk })}\n\n`))
          }
          controller.enqueue(encoder.encode('data: [DONE]\n\n'))
        } catch (err) {
          const msg = err instanceof Error ? err.message : 'AI 响应出错'
          controller.enqueue(
            encoder.encode(`data: ${JSON.stringify({ error: msg })}\n\n`)
          )
        } finally {
          controller.close()
        }
      },
    })

    return new Response(stream, {
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      },
    })
  } catch {
    return Response.json({ error: '请求处理失败' }, { status: 500 })
  }
}
```

- [ ] **Step 2: 提交**

```bash
git add packages/web-app/src/app/api/chat/route.ts
git commit -m "feat: add chat API route with SSE streaming"
```

---

### Task 9: Web App — 聊天对话组件

**Files:**
- Create: `packages/web-app/src/components/chat/ChatContainer.tsx`
- Create: `packages/web-app/src/components/chat/MessageBubble.tsx`
- Create: `packages/web-app/src/components/chat/ChatInput.tsx`
- Create: `packages/web-app/src/lib/api.ts`

- [ ] **Step 1: 创建 API 调用工具**

```typescript
// src/lib/api.ts
import type { Message, LLMConfig } from '@cybersec/shared/types'

export async function sendChatMessage(
  messages: Message[],
  llmConfig: LLMConfig,
  onChunk: (text: string) => void,
  onError: (error: string) => void
): Promise<void> {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, llmConfig }),
  })

  if (!response.ok) {
    const err = await response.json().catch(() => ({ error: '请求失败' }))
    onError(err.error || `HTTP ${response.status}`)
    return
  }

  const reader = response.body?.getReader()
  if (!reader) {
    onError('无法读取响应流')
    return
  }

  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const data = line.slice(6)
      if (data === '[DONE]') return

      try {
        const parsed = JSON.parse(data)
        if (parsed.error) {
          onError(parsed.error)
          return
        }
        if (parsed.content) {
          onChunk(parsed.content)
        }
      } catch {
        // skip malformed chunks
      }
    }
  }
}
```

- [ ] **Step 2: 创建 ChatContainer.tsx**

```tsx
'use client'

import { useState, useRef, useEffect } from 'react'
import { MessageBubble } from './MessageBubble'
import { ChatInput } from './ChatInput'
import { sendChatMessage } from '@/lib/api'
import type { Message, LLMConfig } from '@cybersec/shared/types'

interface ChatContainerProps {
  llmConfig: LLMConfig
}

export function ChatContainer({ llmConfig }: ChatContainerProps) {
  const [messages, setMessages] = useState<Message[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content: '您好，我是**网络安全移动运营商智能 Agent**。\n\n我可以帮您查询：\n- 等保标准要求（如"5G核心网等保三级要求"）\n- 合规法规要点（如"数据安全法对运营商有什么要求"）\n- 行业标准条款（如"YD/T 通信行业安全防护要求"）\n\n请提出您的网络安全合规问题。',
      timestamp: Date.now(),
    },
  ])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = async (content: string) => {
    setError(null)
    const userMsg: Message = {
      id: Date.now().toString(),
      role: 'user',
      content,
      timestamp: Date.now(),
    }

    const assistantMsg: Message = {
      id: (Date.now() + 1).toString(),
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
    }

    setMessages((prev) => [...prev, userMsg, assistantMsg])
    setIsLoading(true)

    const currentMessages = [...messages, userMsg]

    await sendChatMessage(
      currentMessages,
      llmConfig,
      (chunk) => {
        setMessages((prev) => {
          const updated = [...prev]
          const last = updated[updated.length - 1]
          if (last.role === 'assistant') {
            updated[updated.length - 1] = { ...last, content: last.content + chunk }
          }
          return updated
        })
      },
      (err) => {
        setError(err)
        setMessages((prev) => {
          const updated = [...prev]
          updated[updated.length - 1] = {
            ...updated[updated.length - 1],
            content: `抱歉，回答时出错了：${err}`,
          }
          return updated
        })
      }
    )

    setIsLoading(false)
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto px-4 py-6 scrollbar-thin">
        <div className="mx-auto max-w-3xl space-y-4">
          {messages.map((msg) => (
            <MessageBubble key={msg.id} message={msg} />
          ))}
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-600">
              {error}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>
      <ChatInput onSend={handleSend} disabled={isLoading} />
    </div>
  )
}
```

- [ ] **Step 3: 创建 MessageBubble.tsx**

```tsx
'use client'

import { motion } from 'framer-motion'
import type { Message } from '@cybersec/shared/types'
import clsx from 'clsx'

interface MessageBubbleProps {
  message: Message
}

function renderContent(text: string) {
  const lines = text.split('\n')
  return lines.map((line, i) => {
    if (line.startsWith('**') && line.endsWith('**')) {
      return (
        <p key={i} className="font-semibold text-gray-900">
          {line.slice(2, -2)}
        </p>
      )
    }
    if (line.startsWith('- **')) {
      const match = line.match(/- \*\*(.+?)\*\*(.*)/)
      if (match) {
        return (
          <p key={i} className="ml-2 text-gray-700">
            <span className="font-semibold text-primary-600">{match[1]}</span>
            {match[2]}
          </p>
        )
      }
    }
    if (line.startsWith('- ')) {
      return (
        <p key={i} className="ml-2 text-gray-700">
          {line}
        </p>
      )
    }
    if (/^【.+?】/.test(line)) {
      return (
        <p key={i} className="mt-1 font-medium text-primary-700">
          {line}
        </p>
      )
    }
    return (
      <p key={i} className="text-gray-700">
        {line}
      </p>
    )
  })
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
      className={clsx('flex', isUser ? 'justify-end' : 'justify-start')}
    >
      {!isUser && (
        <div className="mr-3 flex h-8 w-8 items-center justify-center rounded-full bg-primary-500 text-sm text-white">
          🛡
        </div>
      )}
      <div
        className={clsx(
          'max-w-[75%] rounded-2xl px-4 py-3 text-sm leading-relaxed',
          isUser
            ? 'bg-primary-50 text-gray-800'
            : 'bg-gray-50 text-gray-800'
        )}
      >
        {renderContent(message.content)}
      </div>
    </motion.div>
  )
}
```

- [ ] **Step 4: 创建 ChatInput.tsx**

```tsx
'use client'

import { useState, useRef } from 'react'
import { Send } from 'lucide-react'

interface ChatInputProps {
  onSend: (content: string) => void
  disabled: boolean
}

export function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [input, setInput] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const handleSubmit = () => {
    const trimmed = input.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setInput('')
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  const handleInput = () => {
    const el = textareaRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, 120) + 'px'
    }
  }

  return (
    <div className="border-t border-gray-100 bg-white px-4 py-3">
      <div className="mx-auto flex max-w-3xl items-end gap-2">
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          placeholder="输入你的网络安全问题..."
          disabled={disabled}
          rows={1}
          className="max-h-[120px] min-h-[44px] flex-1 resize-none rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-900 placeholder-gray-400 outline-none transition-colors focus:border-primary-400 focus:bg-white focus:ring-1 focus:ring-primary-400 disabled:opacity-50"
        />
        <button
          onClick={handleSubmit}
          disabled={disabled || !input.trim()}
          className="flex h-11 w-11 items-center justify-center rounded-xl bg-primary-500 text-white transition-colors hover:bg-primary-600 disabled:opacity-40"
        >
          <Send size={18} />
        </button>
      </div>
    </div>
  )
}
```

- [ ] **Step 5: 提交**

```bash
git add packages/web-app/src/components/ packages/web-app/src/lib/
git commit -m "feat: add chat UI components with streaming rendering"
```

---

### Task 10: Web App — 模型配置面板

**Files:**
- Create: `packages/web-app/src/components/settings/ModelConfigPanel.tsx`
- Create: `packages/web-app/src/components/settings/ModelConfigProvider.tsx`

- [ ] **Step 1: 创建 ModelConfigProvider.tsx**

```tsx
'use client'

import { createContext, useContext, useState, type ReactNode } from 'react'
import { DEFAULT_PROVIDERS } from '@cybersec/shared/types'
import type { LLMConfig } from '@cybersec/shared/types'

interface ModelConfigContext {
  config: LLMConfig
  setConfig: (config: LLMConfig) => void
  showPanel: boolean
  setShowPanel: (show: boolean) => void
}

const defaultConfig: LLMConfig = {
  baseUrl: DEFAULT_PROVIDERS[0].baseUrl,
  apiKey: '',
  model: DEFAULT_PROVIDERS[0].models[0],
  provider: DEFAULT_PROVIDERS[0].name,
}

const ModelConfigCtx = createContext<ModelConfigContext>({
  config: defaultConfig,
  setConfig: () => {},
  showPanel: false,
  setShowPanel: () => {},
})

export function ModelConfigProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<LLMConfig>(defaultConfig)
  const [showPanel, setShowPanel] = useState(true)

  return (
    <ModelConfigCtx.Provider value={{ config, setConfig, showPanel, setShowPanel }}>
      {children}
    </ModelConfigCtx.Provider>
  )
}

export const useModelConfig = () => useContext(ModelConfigCtx)
```

- [ ] **Step 2: 创建 ModelConfigPanel.tsx**

```tsx
'use client'

import { useState } from 'react'
import { DEFAULT_PROVIDERS } from '@cybersec/shared/types'
import { useModelConfig } from './ModelConfigProvider'
import { Settings, ChevronDown, ChevronUp, Check } from 'lucide-react'

export function ModelConfigPanel() {
  const { config, setConfig, showPanel, setShowPanel } = useModelConfig()
  const [selectedProvider, setSelectedProvider] = useState(DEFAULT_PROVIDERS[0].name)

  const provider = DEFAULT_PROVIDERS.find((p) => p.name === selectedProvider) || DEFAULT_PROVIDERS[0]

  const handleProviderChange = (name: string) => {
    setSelectedProvider(name)
    const p = DEFAULT_PROVIDERS.find((pr) => pr.name === name)
    if (p) {
      setConfig({ ...config, baseUrl: p.baseUrl, model: p.models[0], provider: p.name })
    }
  }

  return (
    <div className="border-b border-gray-100 bg-white">
      <button
        onClick={() => setShowPanel(!showPanel)}
        className="flex w-full items-center justify-between px-4 py-2.5 text-sm text-gray-600 hover:bg-gray-50"
      >
        <span className="flex items-center gap-2">
          <Settings size={15} />
          模型配置
          {config.apiKey ? (
            <span className="flex items-center gap-1 text-xs text-green-600">
              <Check size={12} /> 已配置
            </span>
          ) : (
            <span className="text-xs text-amber-600">未配置</span>
          )}
        </span>
        {showPanel ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
      </button>

      {showPanel && (
        <div className="space-y-3 border-t border-gray-50 px-4 py-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-500">选择模型提供商</label>
            <div className="flex gap-2">
              {DEFAULT_PROVIDERS.map((p) => (
                <button
                  key={p.name}
                  onClick={() => handleProviderChange(p.name)}
                  className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                    selectedProvider === p.name
                      ? 'bg-primary-500 text-white'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                  }`}
                >
                  {p.name}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-gray-500">Base URL</label>
            <input
              value={config.baseUrl}
              onChange={(e) => setConfig({ ...config, baseUrl: e.target.value })}
              className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-none focus:border-primary-400 focus:bg-white"
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-gray-500">API Key</label>
            <input
              type="password"
              value={config.apiKey}
              onChange={(e) => setConfig({ ...config, apiKey: e.target.value })}
              placeholder="输入你的 API Key"
              className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-none focus:border-primary-400 focus:bg-white"
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-gray-500">模型</label>
            <select
              value={config.model}
              onChange={(e) => setConfig({ ...config, model: e.target.value })}
              className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-none focus:border-primary-400 focus:bg-white"
            >
              {provider.models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 3: 提交**

```bash
git add packages/web-app/src/components/settings/
git commit -m "feat: add model config panel with provider selector"
```

---

### Task 11: Web App — 主页面

**Files:**
- Create: `packages/web-app/src/app/page.tsx`

- [ ] **Step 1: 创建 page.tsx**

```tsx
'use client'

import { ModelConfigProvider, useModelConfig } from '@/components/settings/ModelConfigProvider'
import { ModelConfigPanel } from '@/components/settings/ModelConfigPanel'
import { ChatContainer } from '@/components/chat/ChatContainer'
import { Shield } from 'lucide-react'

function ChatPageInner() {
  const { config } = useModelConfig()

  return (
    <div className="flex h-screen flex-col bg-white">
      <header className="flex items-center justify-between border-b border-gray-100 bg-white px-4 py-3">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary-500 text-white">
            <Shield size={18} />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-gray-900">网络安全移动运营商智能 Agent</h1>
            <p className="text-xs text-gray-500">合规标准 · 等级保护 · 数据安全 · 行业规范</p>
          </div>
        </div>
      </header>

      <ModelConfigPanel />

      <main className="flex-1 overflow-hidden">
        {!config.apiKey ? (
          <div className="flex h-full items-center justify-center px-4">
            <div className="max-w-md text-center">
              <div className="mb-4 text-4xl">🛡️</div>
              <h2 className="mb-2 text-lg font-semibold text-gray-900">请先配置 API Key</h2>
              <p className="text-sm text-gray-500">在上方"模型配置"面板中选择模型提供商并填写你的 API Key，即可开始使用。</p>
              <div className="mt-4 rounded-lg bg-gray-50 p-4 text-left text-xs text-gray-500">
                <p className="mb-1 font-medium text-gray-700">支持的模型提供商：</p>
                <ul className="list-inside list-disc space-y-0.5">
                  <li>DeepSeek — api.deepseek.com</li>
                  <li>ModelScope（通义系列）— api.modelscope.cn</li>
                  <li>硅基流动 — api.siliconflow.cn</li>
                </ul>
                <p className="mt-2 text-gray-400">也可填写其他兼容 OpenAI API 格式的服务地址。</p>
              </div>
            </div>
          </div>
        ) : (
          <ChatContainer llmConfig={config} />
        )}
      </main>
    </div>
  )
}

export default function HomePage() {
  return (
    <ModelConfigProvider>
      <ChatPageInner />
    </ModelConfigProvider>
  )
}
```

- [ ] **Step 2: 提交**

```bash
git add packages/web-app/src/app/page.tsx
git commit -m "feat: implement main chat page with config flow"
```

---

### Task 12: 安装依赖并验证构建

- [ ] **Step 1: 安装依赖**

```bash
pnpm install
```

- [ ] **Step 2: 构建 agent-core**

```bash
pnpm --filter @cybersec/agent-core build
```

Expected: `packages/agent-core/dist/` 目录生成

- [ ] **Step 3: 构建 web-app**

```bash
pnpm --filter @cybersec/web-app build
```

Expected: `packages/web-app/.next/` 目录生成，构建无错误

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "chore: install dependencies and verify build"
```

---

### Task 13: 创建入口 README（非文档，仅使用说明）

**Files:**
- Create: `README.md`

- [ ] **Step 1: 创建 README.md**

```markdown
# 网络安全移动运营商智能 Agent

基于知识库的移动运营商网络安全管理合规标准问答系统。

## 快速启动

```bash
# 安装依赖
pnpm install

# 构建核心包
pnpm --filter @cybersec/agent-core build

# 启动开发服务器
pnpm dev
```

打开 http://localhost:3000，在"模型配置"面板中选择模型提供商并填写 API Key 后开始使用。

## 内置知识库

涵盖 7 大类网络安全标准：
1. 大标准（网络安全法/数据安全法/个保法）
2. 等级保护 / CII 关键信息基础设施
3. 通信行业标准（YD 系列）
4. 网络安全体系（ISO27000/风险评估）
5. APP 安全要求
6. 系统漏洞管理
7. 安全基线标准（CIS Benchmarks）
```

- [ ] **Step 2: 提交**

```bash
git add README.md
git commit -m "docs: add README with quick start"
```