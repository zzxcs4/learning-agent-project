import type { AgentRunStep, AgentRunTrace, AgentToolCall } from '@/types/chat'

/**
 * Agent loop 轨迹的解析工具。
 *
 * 为什么要单独一层：轨迹有**两个来源**，形状必须收敛到同一个结构后交给 AgentTrace 渲染。
 * 1. **实时**：`step` / `tool_call` / `tool_result` 事件在流式期间被累积（`useSessionStreamSender`）；
 * 2. **回放**：刷新页面后从 `response_payload`（后端落库的 `agent_loop_trace`）读回。
 *
 * 后端两处用的是同一个 helper（`LearningPartnerService._trace_payload`），所以这里
 * 只需要做"防御性解析"——历史数据或异常帧不该让消息渲染崩掉。
 */

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const toNumber = (value: unknown): number | undefined => {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return undefined
}

const toStringArray = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []

const normalizeStep = (raw: unknown): AgentRunStep | null => {
  if (!isRecord(raw)) return null
  return {
    index: toNumber(raw.index),
    kind: typeof raw.kind === 'string' ? raw.kind : undefined,
    elapsed_ms: toNumber(raw.elapsed_ms),
    tool_calls: toStringArray(raw.tool_calls),
    text_preview: typeof raw.text_preview === 'string' ? raw.text_preview : undefined,
  }
}

const normalizeToolCall = (raw: unknown): AgentToolCall | null => {
  if (!isRecord(raw)) return null
  const name = typeof raw.name === 'string' ? raw.name : ''
  if (!name) return null
  return {
    name,
    args: isRecord(raw.args) ? raw.args : undefined,
    // 后端 300 字符截断；只要不是布尔/字符串就丢掉，避免脏数据把渲染带崩
    ok: typeof raw.ok === 'boolean' ? raw.ok : undefined,
    elapsed_ms: toNumber(raw.elapsed_ms),
    result_preview: typeof raw.result_preview === 'string' ? raw.result_preview : undefined,
  }
}

/** 把任意来源的轨迹收敛成 `AgentRunTrace`；无法识别时返回 null（调用方据此不渲染）。 */
export function normalizeAgentTrace(raw: unknown): AgentRunTrace | null {
  if (!isRecord(raw)) return null

  const steps = Array.isArray(raw.steps)
    ? raw.steps.map(normalizeStep).filter((step): step is AgentRunStep => step !== null)
    : []
  const toolCalls = Array.isArray(raw.tool_calls)
    ? raw.tool_calls.map(normalizeToolCall).filter((call): call is AgentToolCall => call !== null)
    : []

  // 没有步骤也没有工具调用时不算轨迹：单轮聊天不该显示空的"执行轨迹"
  if (!steps.length && !toolCalls.length) return null

  return {
    type: typeof raw.type === 'string' ? raw.type : 'agent_loop_trace',
    stop_reason: typeof raw.stop_reason === 'string' ? raw.stop_reason : undefined,
    steps,
    tool_calls: toolCalls,
  }
}

/** 从消息的 `responsePayload` 里取回放的轨迹（后端落库结构）。 */
export function traceFromResponsePayload(
  payload?: Record<string, unknown> | null,
): AgentRunTrace | null {
  if (!isRecord(payload)) return null
  if (payload.type !== 'agent_loop_trace') return null
  return normalizeAgentTrace(payload)
}

const TOOL_LABELS: Record<string, string> = {
  search_material: '检索知识库',
  get_mastery: '查询掌握度',
  get_weak_points: '查询薄弱点',
  list_practice_history: '查询练习历史',
  save_note: '保存笔记',
  delegate_quiz: '委派：出题',
  delegate_evaluate: '委派：评测',
  delegate_explain: '委派：难点讲解',
  delegate_archive: '委派：笔记归档',
}

/** MCP 工具名的前缀（后端 `app/agent/mcp/client.py` 生成，两端必须一致）。 */
export const MCP_TOOL_PREFIX = 'mcp_'

/** 是否来自外部 MCP server 的工具（只看名字前缀，不需要额外的映射表）。 */
export function isMcpCall(name: string): boolean {
  return name.startsWith(MCP_TOOL_PREFIX)
}

/** 工具/worker 的展示名：中文可读优先，未知名字原样显示（不隐藏信息）。
 *
 * MCP 工具**刻意不拆成"server / tool"**：名字是 `mcp_<server>_<tool>`，
 * 而 server 名里本身可以带下划线（`my_server`），前端拆就一定会拆错。
 * 所以这里只去掉前缀、保留 `server_tool` 原文，由徽标表明它来自 MCP。
 */
export function toolLabel(name: string): string {
  if (isMcpCall(name)) return name.slice(MCP_TOOL_PREFIX.length)
  return TOOL_LABELS[name] ?? name
}

/** `delegate_*` 是委派给 worker（有角色人格），其余是原子工具——展示上要能区分。 */
export function isWorkerCall(name: string): boolean {
  return name.startsWith('delegate_')
}

/** 把工具参数渲染成简短的可读文本，供时间线里展示。 */
export function formatToolArgs(args?: Record<string, unknown>): string {
  if (!args) return ''
  const parts: string[] = []
  for (const [key, value] of Object.entries(args)) {
    if (value === null || value === undefined || value === '') continue
    let text: string
    if (typeof value === 'string') {
      text = value
    } else if (Array.isArray(value)) {
      text = `[${value.length} 项]`
    } else if (typeof value === 'object') {
      text = '{…}'
    } else {
      text = String(value)
    }
    if (text.length > 60) text = `${text.slice(0, 60)}…`
    parts.push(`${key}=${text}`)
  }
  return parts.join(' · ')
}

/** 轨迹的汇总文案（用于折叠标题）。 */
export function traceSummary(trace: AgentRunTrace): string {
  const toolCount = trace.tool_calls.length
  const mcpCount = trace.tool_calls.filter((call) => isMcpCall(call.name)).length
  const failedCount = trace.tool_calls.filter((call) => call.ok === false).length
  const parts: string[] = []
  if (toolCount) parts.push(`${toolCount} 次工具/委派`)
  // MCP 与失败次数单独说：这两件事最容易被"看起来跑完了"掩盖
  if (mcpCount) parts.push(`含 ${mcpCount} 次 MCP`)
  if (failedCount) parts.push(`${failedCount} 次失败`)
  parts.push(`${trace.steps.length} 步`)
  if (trace.stop_reason === 'max_steps') parts.push('步数用尽')
  return parts.join(' · ')
}
