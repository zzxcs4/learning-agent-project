export type SegmentType = 'file' | 'image' | 'audio'

export type MessageSegment = {
  type: SegmentType
  name?: string
  url?: string
  resource_id?: number
}

export type KnowledgeSource = {
  file_name: string
  score: number
  chunk_index?: number
  resource_id?: number
  text?: string
  text_preview?: string
}

export type AgentOption = {
  label: string
  value: string
}

export type ChatSendPayload = {
  text: string
  agentName?: string
  /** 出题时绑定的知识库（不传 = 不注入考纲，只按 request_text 出题） */
  knowledgeBaseId?: number
  /** 出题发散度 1–5（默认 3）：1 只考核心与已知弱点，5 优先冷门/跨章综合 */
  divergence?: number
}

export type LearningQuizOption = {
  value: string
  label: string
}

export type LearningQuizQuestion = {
  id: string
  type: 'radio' | 'checkbox' | 'textarea'
  stem: string
  options: LearningQuizOption[]
  required?: boolean
  points?: number
}

export type LearningQuiz = {
  title?: string
  description?: string
  question_count?: number
  questions: LearningQuizQuestion[]
}

export type LearningQuizPayload = {
  type?: string
  version?: number
  run_id?: string
  /** awaiting_params = 只写了参数卡还没出题；waiting_answer = 题目已就绪；completed = 已评出结果 */
  status?: string
  quiz?: LearningQuiz
  /** 参数没齐时的参数卡规格（后端 `quiz_params_service.build_params_spec` 产出）。
   *
   * 控件怎么画、有哪些档位、默认值多少**全部由后端给**：前端只做泛型渲染，
   * 这样"题目数 5–20""发散度五档文案"这类口径只有一处真相，改后端就够。
   */
  params?: LearningQuizParamsSpec
  // ---- 评分结果（status=completed 时后端随 payload 一起落库）----
  // 这样一条出题消息在"刚交卷实时看"和"事后刷新从历史再打开"能渲染出同一份结果，
  // 而不是只在内存里活一次、关掉就再也看不到（用户 2026-10-19 报的问题）。
  score?: number
  evaluation?: {
    score?: number
    items?: {
      question_id: string
      score?: number
      is_correct?: boolean | null
      correct_answer?: string
      analysis?: string
    }[]
  }
  weaknesses?: string[]
  review?: string
  reflection?: unknown
  /** 提交时的作答（`{question_id, value}[]`），刷新恢复"我的答案"用 */
  answers?: { question_id: string; value: unknown }[]
}

/** 参数卡里的一个字段（对应后端 spec.fields[i]） */
export type LearningQuizParamsField = {
  key: string
  label: string
  /** select=下拉（知识库）/ number=数字（题目数）/ radio=单选框（发散度、难度） */
  type: 'select' | 'number' | 'radio' | string
  /** 表单初值（可能为空 = 需要用户选） */
  value: unknown
  /** 用户没选时的回落值 */
  default?: unknown
  min?: number
  max?: number
  /** 单选项；select 时是候选知识库 */
  options?: LearningQuizParamsOption[]
  /** true = 后端已有权威值（展示即可，不必再问） */
  resolved?: boolean
  /** 给用户的一句说明，例如「你只有一个知识库，已自动使用「X」」 */
  note?: string
}

export type LearningQuizParamsOption = {
  value: string | number
  label: string
  description?: string
  /** 知识库候选带文档数（0 表示还没上传资料） */
  resource_count?: number
}

/** 参数卡规格：fields 是全部字段，missing 是**还需要问**的字段 key */
export type LearningQuizParamsSpec = {
  fields: LearningQuizParamsField[]
  missing?: string[]
}

/** 确认出题参数时提交的四个值（都可空：空的走后端默认） */
export type LearningWorkflowParamsSubmit = {
  question_count?: number
  knowledge_base_id?: number
  divergence?: number
  difficulty?: string
}

/** ---------- 难点讲解：知识卡（第一期：卡结构 + 可选配图） ---------- */

export type KnowledgeCardPoint = {
  /** 难点小标题（后端已限长，前端不再截断） */
  title: string
  /** 一句要点 */
  tip: string
}

export type KnowledgeCard = {
  title: string
  topic: string
  summary: string
  points: KnowledgeCardPoint[]
  /** 最常见误区（后端把多条合成一句话，所以这里是字符串） */
  pitfall: string
  correct: string
  /** 画面建议：只给出图用，前端不展示（留在载荷里便于排查"图画得不对"） */
  visual_hint?: string
}

/** 知识卡消息的 `response_payload` 形状（`type = "difficulty_card"`）。
 *
 * `degraded=true` 表示这次**没有配图**（没配 Seedream Key / 出图失败）：卡结构照常有，
 * 前端要给一句「本次未生成配图」的说明，而不是让用户以为图加载失败了。
 */
export type KnowledgeCardPayload = {
  type?: string
  version?: number
  run_id?: string
  status?: string
  card?: KnowledgeCard
  image_url?: string | null
  resource_id?: number | null
  degraded?: boolean
  /** 没出图时的**可行动原因**（后端把方舟错误码翻成人话）。
   *
   * 后端只在 `degraded=true` 时给值：额度耗尽、模型未开通、模型 ID 写错、没配 Key
   * 都会给出各自不同的说明。有它才能告诉用户"该去做什么"，而不是笼统一句"调用失败"。
   */
  degraded_reason?: string | null
}

export type SessionItem = {
  id: number
  session_model: number
  title: string
  created_at: number
}

export type SessionListResponse = {
  page: number
  page_size: number
  total: number
  has_more: boolean
  sessions: SessionItem[]
}

export type SessionRecord = {
  id: number
  request_id: string
  status: number
  request_text: string
  response_text: string
  request_segments: MessageSegment[]
  response_segments: MessageSegment[]
  response_payload?: Record<string, unknown> | null
  sources?: KnowledgeSource[]
  created_at: number
}

export type SessionMessagesResponse = {
  session_id: number
  page: number
  page_size: number
  has_more: boolean
  messages: SessionRecord[]
}

export type UiMessage = {
  id: string
  role: 'assistant' | 'user'
  text: string
  createdAt: number
  segments: MessageSegment[]
  requestId?: string
  responsePayload?: Record<string, unknown> | null
  sources?: KnowledgeSource[]
  status?: number
  /** agent loop 的执行轨迹（流式期间累积，刷新后从 responsePayload 读回） */
  trace?: AgentRunTrace
  /** 本轮回答是否还在流式生成中（气泡内联显示阶段提示与光标，不用全屏遮罩） */
  streaming?: boolean
  /** 流式期间的阶段文案，例如「正在调用 检索知识库…」 */
  streamStatus?: string
  /** 这条题目消息是**本页会话刚出的题**：挂载后自动把答题弹窗弹出来。
   *
   * 只对"刚生成的那条"置位——从历史加载/切会话回来的消息一律不置位，
   * 否则一个会话里有几套题就会同时弹出几个弹窗（用户 2026-10-19 报的问题）。
   */
  autoOpenQuiz?: boolean
  /** 思考型模型的推理过程（`reasoning` 事件累积）。
   *
   * **只存在于流式期间**：后端不落库（一段推理几千字，混进 response_text 会污染答案），
   * 所以刷新页面后这里就是空的——这是有意的取舍，不是漏写。
   */
  reasoning?: string
}

// ---------------------------------------------------------------------------
// Agent loop 执行轨迹（第 2 期）
//
// 形状与后端 `LearningPartnerService._trace_payload()` **逐字段对齐**：
// done 事件的 `trace` 字段与落库的 `response_payload` 是同一个结构，
// 所以实时渲染与刷新后回显共用一套解析（见 `normalizeAgentTrace`）。
// ---------------------------------------------------------------------------

/** 一次工具调用的记录（对应后端 LoopResult.tool_calls）。
 *
 * `ok` / `elapsed_ms` / `result_preview` 是第 3 期补上的：此前轨迹里只有名字与参数，
 * 于是**工具失败在界面上与成功完全一样**（`ToolRuntime` 把异常收敛成观察结果，
 * 调用本身不会失败），刷新后的回放更是连成功与否都无从判断。
 */
export type AgentToolCall = {
  name: string
  args?: Record<string, unknown>
  /** 工具/委派是否成功（失败时 ToolRuntime 会把异常转成一段 error JSON） */
  ok?: boolean
  elapsed_ms?: number
  /** 结果预览（后端截断到 300 字符），用来回答"它到底查到了什么" */
  result_preview?: string
}

/** 一个 loop 步骤（一次模型调用 = 一步）。 */
export type AgentRunStep = {
  index?: number
  /** think=模型在推理；tool=这一步要求调工具；final=这一步给出了最终回答 */
  kind?: 'think' | 'tool' | 'final' | string
  elapsed_ms?: number
  tool_calls?: string[]
  text_preview?: string
}

/** 工具结果的实时事件载荷（不进落库结构，只在流式期间展示）。 */
export type AgentToolResult = {
  name: string
  ok: boolean
  elapsed_ms?: number | null
  result_preview?: string
  /** 出题委派（`delegate_quiz`）才有：worker 落库的出题消息认领键
   * （消息 `request_id` 是 `"{run_id}:quiz"`），前端靠它只对本次新出的题自动弹窗。 */
  run_id?: string
}

export type AgentRunTrace = {
  /** 后端固定为 agent_loop_trace */
  type?: string
  /** final_answer=正常收敛；max_steps=步数用尽 */
  stop_reason?: string
  steps: AgentRunStep[]
  tool_calls: AgentToolCall[]
}
