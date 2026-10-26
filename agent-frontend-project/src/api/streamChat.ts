import { getAccessToken } from './token'
import type {
  AgentRunStep,
  AgentRunTrace,
  AgentToolResult,
  KnowledgeSource,
} from '@/types/chat'

export type AgentTaskResult = {
  route_type: string
  target_agent?: string | null
  workflow_type?: string | null
  run_id?: string | null
  status: string
  data: Record<string, unknown>
}

// 与后端约定的 SSE 消息类型。
//
// `loop_start` 之后的四个是第 2 期 agent loop 新增的**增量事件**：
// start(带 mode) → step → tool_call → tool_result → … → delta → done
// 老客户端不认识它们时会走 `default` 分支忽略（解析器不做穷尽校验），所以是向后兼容的新增。
//
// `reasoning`（第 14 轮）是思考型模型的**推理**增量：与 `delta` 同源同频，但**不是答案**。
// 后端把它单独成事件（不进 response_text、不落库），前端只用来做"思考中"的可视进度。
type StreamEvent =
  | { type: 'start'; message_id: number; mode?: 'agent_loop' | string }
  | { type: 'delta'; text: string }
  | { type: 'reasoning'; text: string }
  | {
      type: 'done'
      response_text: string
      sources?: KnowledgeSource[] | null
      /** loop 才有：收敛原因（final_answer / max_steps） */
      stop_reason?: string
      /** loop 才有：扁平步骤摘要（保留字段） */
      steps?: AgentRunStep[]
      /** loop 才有：完整轨迹，形状与落库的 response_payload 一致 */
      trace?: AgentRunTrace
    }
  | { type: 'error'; message: string }
  | { type: 'step'; index: number; kind: string; text?: string }
  | { type: 'tool_call'; name: string; args?: Record<string, unknown>; step?: number }
  | ({ type: 'tool_result' } & AgentToolResult)

/** start 事件里标记"这一轮走的是 agent loop"的取值（后端 LearningPartnerService 写入）。 */
export const AGENT_LOOP_MODE = 'agent_loop'

// 发起总控任务所需参数；总控可能返回 SSE，也可能返回工作流 JSON。
type StreamChatOptions = {
  sessionId: number
  sessionModel: 0 | 2
  requestText: string
  agentName?: string
  selectModel?: number
  questionCount?: number
  provider?: string
  /** 出题绑定的知识库；不传则后端不注入考纲 */
  knowledgeBaseId?: number
  /** 出题发散度 1–5；不传则后端按默认 3 */
  divergence?: number
  onEvent: (event: StreamEvent) => void
}

// 按 \n\n 拆分 buffer，frames 为完整帧，rest 为剩余未完整部分
function parseSseFrames(buffer: string): { frames: string[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, '\n')
  const frames = normalized.split('\n\n')
  const rest = frames.pop() ?? ''
  return { frames, rest }
}

// 获取帧中所有 data: 行，并拼成字符串
function parseSseData(frame: string): string | null {
  const dataLines = frame
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.startsWith('data:'))
  if (!dataLines.length) {
    return null
  }
  return dataLines.map((line) => line.slice(5).trim()).join('\n')
}

// 读取后端错误信息，优先 message 字段，否则用状态码
async function readErrorMessage(response: Response): Promise<string> {
  const fallback = `request failed: ${response.status}`
  try {
    const payload = await response.clone().json()
    if (payload?.message) {
      return String(payload.message)
    }
  } catch {}
  return fallback
}

async function readStream(response: Response, onEvent: (event: StreamEvent) => void): Promise<void> {
  if (!response.body) {
    throw new Error('stream response body is empty')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  // 持续读取流式数据并触发回调
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const { frames, rest } = parseSseFrames(buffer)
    buffer = rest
    for (const frame of frames) {
      const data = parseSseData(frame)
      if (!data) continue
      try {
        onEvent(JSON.parse(data) as StreamEvent)
      } catch {
        // 忽略异常帧，避免中断整个流
      }
    }
  }
}

// 发起总控任务：返回 JSON 时交给上层按 route_type/workflow_type 扩展处理；返回 SSE 时沿用原流式回调。
export async function streamChat(options: StreamChatOptions): Promise<AgentTaskResult | void> {
    const base = (import.meta.env.VITE_API_BASE_URL as string | undefined) || '/api'
    const baseUrl = base.endsWith('/') ? base.slice(0, -1) : base
    const url = `${baseUrl}/agent/tasks/start`

    // 旧版直连流式聊天接口，保留给后续同学参考：
    // const url = `${baseUrl}/sessions/${options.sessionId}/stream-chat?session_model=${options.sessionModel}`

    const token = getAccessToken()
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {})
      },
      body: JSON.stringify({
        session_id: options.sessionId,
        session_model: options.sessionModel,
        request_text: options.requestText,
        agent_name: options.agentName,
        select_model: options.selectModel ?? 0,
        question_count: options.questionCount,
        // 知识库：**各路由都有意义**（这里曾写着"只在出题链路有意义；后端对其它路由会忽略"，
        // 那句话在难点讲解接入后就错了）。目前三处用途：出题注入考纲并决定归属、
        // 默认模式给 loop 的 `search_material` 划检索范围、难点讲解按用户这句话检索原文+取考纲。
        knowledge_base_id: options.knowledgeBaseId ?? null,
        divergence: options.divergence,
        provider: options.provider ?? 'deepseek'
      })
    })

    // 旧版直连流式聊天 body，保留给后续同学参考：
    // body: JSON.stringify({
    //   request_text: options.requestText,
    //   select_model: options.selectModel ?? 0,
    //   provider: options.provider ?? 'deepseek'
    // })

    if (!response.ok) {
      throw new Error(await readErrorMessage(response))
    }

    const contentType = response.headers.get('content-type') || ''
    if (contentType.includes('application/json')) {
      const payload = await response.json()
      return payload.data as AgentTaskResult
    }

    await readStream(response, options.onEvent)
}
