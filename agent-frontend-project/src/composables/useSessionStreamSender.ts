import { ref } from 'vue'
import { streamChat, type AgentTaskResult } from '@/api/streamChat'
import { toolLabel } from '@/utils/agentTrace'
import type {
  AgentRunStep,
  AgentRunTrace,
  AgentToolCall,
  ChatSendPayload,
  KnowledgeSource,
} from '@/types/chat'

/**
 * 流式阶段状态：由 sender 推给气泡做**内联**提示。
 *
 * 为什么不用 ElLoading 全屏遮罩（第 12 轮改掉的做法）：`ElLoading.service({ lock: true })`
 * 铺满整个视口，流式增量虽然一直在往消息里写，但**被遮罩盖住看不见**；遮罩只在整条流
 * 结束的 `finally` 里才关，于是用户按回车后看到的是"整页加载中"，等遮罩撤掉时全文已经
 * 一次性写完了——流式等于白做。阶段提示必须进气泡，才能和增量文字一起被看到。
 */
export type StreamStatus = {
  /** 是否仍在生成（false = 本轮结束，气泡要撤掉提示与光标） */
  streaming: boolean
  /** 阶段文案，如「正在调用 检索知识库…」；streaming=false 时为空串 */
  text: string
}

// 用于写入流式消息内容的接口
type PendingWriter = {
  appendDelta: (delta: string) => void // 追加流式响应内容
  finish: (fullText: string, sources?: KnowledgeSource[] | null) => void // 流式响应结束时调用
  /** 可选：实时轨迹回调（agent loop 才有），不实现则忽略 */
  onTrace?: (trace: AgentRunTrace) => void
  /** 可选：流式状态回调（进行中/阶段文案），不实现则忽略 */
  onStatus?: (status: StreamStatus) => void
  /** 可选：思考型模型的推理增量（只做可视进度，不是答案），不实现则忽略 */
  appendReasoning?: (delta: string) => void
}

// useSessionStreamSender 选项参数类型
type UseSessionStreamSenderOptions = {
  sessionModel: 0 | 2 // 会话模型类型
  getActiveSessionId: () => number | null // 获取当前会话ID
  createPending: (text: string, sessionId: number) => PendingWriter // 生成待发送消息的写入器
  syncAfterDone?: (sessionId: number, result?: AgentTaskResult) => Promise<void> // 可选，消息发送完触发同步
  onError?: (message: string) => void // 可选，错误时回调
  /** 可选：本轮**委派了出题 worker** 时回调（带 worker 落库的 run_id 列表）。
   *
   * 为什么单独需要它：出题消息是 worker 自己写进会话的（`request_id = "{run_id}:quiz"`），
   * 而 SSE（agent loop）路径按设计不回刷消息列表（done 已带回权威全文），于是那条题目消息
   * 在用户看来"根本不存在"——只看到一段自然语言"题目出好了"，没有答题弹窗。
   * 所以只在**真的出了题**时补一次回刷，其余 SSE 路径的零回刷口径不变。
   */
  onQuizGenerated?: (sessionId: number, runIds: string[]) => Promise<void> | void
  /** 可选：本轮**委派了难点讲解 worker** 时回调（带知识卡消息的 run_id 列表）。
   *
   * 与出题同因不同果：知识卡消息（`request_id = "{run_id}:difficulty"`）也是 worker
   * 自己落库的，不认领就看不到卡与配图，只看到一句"讲好了"。与出题的区别是它**不弹窗**：
   * 卡本身就是普通的图片附件消息，回刷即渲染。
   */
  onDifficultyCardGenerated?: (sessionId: number, runIds: string[]) => Promise<void> | void
}

const normalizePayload = (payload: string | ChatSendPayload): ChatSendPayload => {
  if (typeof payload === 'string') {
    return { text: payload }
  }
  return payload
}

const getAgentPendingText = (agentName?: string) => {
  if (agentName === 'quiz_generate_workflow') return '正在生成学习测评题目…'
  // 难点讲解要跑"结构化 + 出图 + 存对象存储"，几十秒很正常，文案要说清在干什么
  if (agentName === 'difficulty_learner') return '正在生成知识卡（含配图，可能要几十秒）…'
  if (agentName) return '智能体正在运行…'
  // 第 2 期起"不选智能体"= 默认进 agent loop，它要跑好几秒（检索/委派都可能发生）。
  // 这里必须给提示：否则气泡在整段 loop 期间空着，用户会以为卡住。
  return '学习搭档正在处理…'
}

/** loop 期间累积轨迹：step / tool_call / tool_result 三类事件拼成一个 AgentRunTrace。 */
const createTraceAccumulator = () => {
  const steps: AgentRunStep[] = []
  const toolCalls: AgentToolCall[] = []
  let stopReason: string | undefined

  // 快照函数**声明在对象之前**：对象字面量里的方法用简写语法时没有自己的词法名字可用，
  // 在方法体内直接写 `snapshot()` 解析不到（tsc 报 Cannot find name），必须走外部常量。
  const snapshot = (): AgentRunTrace => ({
    type: 'agent_loop_trace',
    stop_reason: stopReason,
    // 浅拷贝一层：AgentTrace 是 reactive 渲染的，直接给出内部数组会让 Vue 追踪到累积过程，
    // 也让调用方无法误改累积器的状态。
    steps: steps.map((step) => ({ ...step, tool_calls: [...(step.tool_calls ?? [])] })),
    tool_calls: toolCalls.map((call) => ({ ...call })),
  })

  return {
    onStep(event: { index: number; kind: string }): AgentRunTrace {
      steps.push({ index: event.index, kind: event.kind, tool_calls: [] })
      return snapshot()
    },
    onToolCall(event: { name: string; args?: Record<string, unknown>; step?: number }): AgentRunTrace {
      toolCalls.push({ name: event.name, args: event.args })
      // 挂到"那一步"上：后端 tool_call 事件带 step（步号从 1 开始），缺了就退化成"最近一步"。
      const index = typeof event.step === 'number' ? event.step - 1 : steps.length - 1
      const current = index >= 0 && index < steps.length ? steps[index] : undefined
      if (current) {
        current.tool_calls = [...(current.tool_calls ?? []), event.name]
        // 后端流式期间的 step 事件统一发 kind=think（那一刻还不知道要不要调工具），
        // 权威 kind 只在 done.trace 里。这里把"确实要调工具"的这一步就地升级，
        // 否则整段流里每一步都显示"推理中"，结束瞬间又全变成"调用工具/给出回答"。
        current.kind = 'tool'
      }
      return snapshot()
    },
    onToolResult(event: {
      name: string
      ok?: boolean
      elapsed_ms?: number | null
      result_preview?: string
    }): AgentRunTrace {
      // 把成败/耗时/结果预览补记到最近一个"还没有结果"的同名调用上。
      // 按顺序配对（而不是按名字覆盖）是因为同名工具可能被调多次。
      // 注意**不要**顺手改 step.elapsed_ms：那是"这一步模型的耗时"，
      // 拿工具耗时顶上去会让实时数字与 done.trace 里的权威值不一致（刷新前后不一样）。
      for (let i = toolCalls.length - 1; i >= 0; i -= 1) {
        const call = toolCalls[i]
        if (call && call.name === event.name && call.ok === undefined) {
          if (typeof event.ok === 'boolean') call.ok = event.ok
          if (typeof event.elapsed_ms === 'number') call.elapsed_ms = event.elapsed_ms
          if (typeof event.result_preview === 'string') call.result_preview = event.result_preview
          break
        }
      }
      return snapshot()
    },
    /** done 事件带回了权威 stop_reason，补上后再取快照。 */
    withStopReason(reason?: string): AgentRunTrace {
      stopReason = reason
      return snapshot()
    },
    /** 累积器里到底有没有真东西（步骤或工具调用）。 */
    hasContent(): boolean {
      return steps.length > 0 || toolCalls.length > 0
    },
    snapshot,
  }
}

// 用于发送聊天流式消息的逻辑复用 Hook
export function useSessionStreamSender(options: UseSessionStreamSenderOptions) {
  // 标记当前是否处于发送中
  const sending = ref(false)
  // 记录当前会话的流请求代号，自增用于中止控制
  const streamEpoch = ref(0)
  // 当前在飞流的"撤销内联提示"钩子。stopActiveStream 只是让流回调失效（不中断 fetch），
  // finally 要等这个请求真正返回才跑，所以被打断时得由 stopActiveStream 主动撤掉提示，
  // 否则气泡会一直留着"正在作答"的光标，看起来像卡死。
  let clearActiveStatus: (() => void) | null = null

  // 停止当前激活的流（通过自增 epoch，让未来的流回调失效）
  const stopActiveStream = () => {
    streamEpoch.value += 1
    sending.value = false
    clearActiveStatus?.()
    clearActiveStatus = null
  }

  // 发送流式请求
  const send = async (payload: string | ChatSendPayload) => {
    const { text, agentName, knowledgeBaseId, divergence } = normalizePayload(payload)
    const sessionId = options.getActiveSessionId()
    // 未选中会话或已有请求在发送中，直接返回
    if (!sessionId || sending.value) {
      return
    }

    // 标记当前请求的 epoch，确保流回调只响应最新的请求
    const requestEpoch = streamEpoch.value + 1
    streamEpoch.value = requestEpoch
    sending.value = true

    // 创建"占位消息"写入控制器
    const pending = options.createPending(text, sessionId)
    // 阶段提示一律走**气泡内联**：全屏遮罩会把流式文字一起盖住（见 StreamStatus 的说明）。
    const setStatus = (streaming: boolean, statusText: string) => {
      pending.onStatus?.({ streaming, text: streaming ? statusText : '' })
    }
    clearActiveStatus = () => setStatus(false, '')
    // 首帧立刻给提示：多数 loop 在第一次 delta 之前要跑好几秒检索/工具调用。
    setStatus(true, getAgentPendingText(agentName))
    // 每个请求独立的轨迹累积器：实时把 step/tool_call/tool_result 拼成 AgentRunTrace
    const trace = createTraceAccumulator()
    // 是否已经开始输出正文：用来避免每个 delta 都重复写一次同一个阶段文案
    let answering = false
    // 本轮委派出去、已经落库的出题 run_id（后端在 delegate_quiz 的 tool_result 事件里回推）
    const quizRunIds: string[] = []
    // 同理：本轮委派出去、已经落库的**知识卡** run_id（delegate_explain 回推）
    const difficultyRunIds: string[] = []

    try {
      const result = await streamChat({
        sessionId,
        sessionModel: options.sessionModel,
        requestText: text,
        agentName,
        knowledgeBaseId,
        divergence,
        onEvent: (event) => {
          // 非本轮请求或会话已变化时跳过，防止旧流回调串写
          if (requestEpoch !== streamEpoch.value || options.getActiveSessionId() !== sessionId) {
            return
          }
          // 思考型模型的推理增量：不是答案，只进"思考中"块。
          // 它通常比正文早到好几秒（实测一句话的回答：首个推理在 t=0.03s、
          // 首个正文在 t=2.91s），所以这段是**唯一**能让等待期有内容可看的东西。
          //
          // 故意**不改**阶段文案：`step` 事件已经把状态置为「正在推理…」，
          // 再切一个同义文案只是闪一下，没有信息量（进度感由思考块本身提供）。
          if (event.type === 'reasoning') {
            pending.appendReasoning?.(event.text || '')
          }
          if (event.type === 'delta') {
            if (!answering) {
              answering = true
              setStatus(true, '正在作答…')
            }
            pending.appendDelta(event.text || '')
          }
          // agent loop 的增量事件：累积后回吐快照，消息气泡即可实时显示步骤时间线
          if (event.type === 'step') {
            setStatus(true, '正在推理…')
            pending.onTrace?.(trace.onStep(event))
          }
          if (event.type === 'tool_call') {
            setStatus(true, `正在调用 ${toolLabel(event.name)}…`)
            pending.onTrace?.(trace.onToolCall(event))
          }
          if (event.type === 'tool_result') {
            setStatus(
              true,
              event.ok === false
                ? `${toolLabel(event.name)} 未成功，继续推理…`
                : `${toolLabel(event.name)} 已返回，继续推理…`,
            )
            pending.onTrace?.(trace.onToolResult(event))
            // 出题委派：记下 run_id，流结束后要专门回刷一次消息列表（见 onQuizGenerated）
            if (event.name === 'delegate_quiz' && event.ok !== false && event.run_id) {
              quizRunIds.push(event.run_id)
            }
            // 难点讲解委派：知识卡消息同样是 worker 落库的，同样要认领（见 onDifficultyCardGenerated）
            if (event.name === 'delegate_explain' && event.ok !== false && event.run_id) {
              difficultyRunIds.push(event.run_id)
            }
          }
          if (event.type === 'done') {
            // done 里的 trace 是权威结果（含 stop_reason）；缺失时回退到实时累积的快照。
            // 但**只有真有内容才推**：普通单轮问答（没有 step/tool_call 事件）走的是同一条
            // done 分支，无条件推会得到一个 `{steps:[],tool_calls:[]}` —— 它在气泡里是真值，
            // 于是每一条普通回答下面都挂着一个空的「执行轨迹 · 0 步」框。
            const finalTrace = event.trace ?? trace.withStopReason(event.stop_reason)
            if (event.trace || trace.hasContent()) {
              pending.onTrace?.(finalTrace)
            }
            pending.finish(event.response_text || '', event.sources)
          }
          if (event.type === 'error') {
            options.onError?.(event.message || '流式请求失败')
          }
        }
      })
      if (result && requestEpoch === streamEpoch.value && options.getActiveSessionId() === sessionId) {
        // JSON 工作流路径（出题评测 / 难点讲解）：回刷一次并带上 run_id，让前端认领本次新出的内容
        await options.syncAfterDone?.(sessionId, result)
      } else if (
        (quizRunIds.length || difficultyRunIds.length) &&
        requestEpoch === streamEpoch.value &&
        options.getActiveSessionId() === sessionId
      ) {
        // SSE 路径（agent loop）：只有真的委派了出题/讲解才回刷，普通问答仍然零回刷
        if (quizRunIds.length) {
          await options.onQuizGenerated?.(sessionId, quizRunIds)
        }
        if (difficultyRunIds.length) {
          await options.onDifficultyCardGenerated?.(sessionId, difficultyRunIds)
        }
      }
    } catch (error) {
      const err = error as Error
      // 只处理本轮请求的错误
      if (requestEpoch === streamEpoch.value) {
        options.onError?.(err.message || '流式请求失败')
      }
    } finally {
      // 先撤掉内联提示：正常 done、error、被打断三条路径都必须让气泡回到静止态。
      if (clearActiveStatus) {
        clearActiveStatus()
        clearActiveStatus = null
      }
      // 不是当前流，直接返回避免重入
      if (requestEpoch !== streamEpoch.value) {
        return
      }
      sending.value = false
    }
  }

  // 导出 sending 标记、停止流和发送主逻辑
  return {
    sending,
    stopActiveStream,
    send
  }
}
