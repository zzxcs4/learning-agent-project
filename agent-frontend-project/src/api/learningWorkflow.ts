import api, { type ApiResponse } from './http'
import type { LearningQuiz, LearningWorkflowParamsSubmit } from '../types/chat'

export type LearningAnswerItem = {
  question_id: string
  value: unknown
}

/** 单题评分明细：与后端评测节点返回的 items 对齐 */
export type LearningEvaluationItem = {
  question_id: string
  score?: number
  is_correct?: boolean | null
  correct_answer?: string
  analysis?: string
}

/** 评测结果：score + 逐题 items */
export type LearningEvaluation = {
  score?: number
  items?: LearningEvaluationItem[]
}

/** 反思结论里的单条判据（verdict 白名单与推导口径见后端 reflection_service） */
export type LearningReflectionVerdict = {
  verdict: string
  [key: string]: unknown
}

/**
 * 评测后反思（升级规划 §7.2）：对**本轮出题质量**的自评，判据双向。
 * `overall` 由后端从四条判据推导：pass / warn / unknown（证据不足）/ skipped（自评未完成）。
 */
export type LearningReflection = {
  overall?: string
  reason?: string
  weak_point_coverage?: LearningReflectionVerdict & { covered?: string[]; missed?: string[] }
  chapter_rotation?: LearningReflectionVerdict & { chapters?: string[] }
  duplication?: LearningReflectionVerdict & { duplicates?: string[] }
  difficulty?: LearningReflectionVerdict & { evidence?: string }
  suggestions?: string[]
  evidence?: Record<string, unknown>
}

export type LearningWorkflowSubmitResponse = {
  run_id: string
  status: string
  score: number
  evaluation: LearningEvaluation
  weaknesses: string[]
  review: string
  /** 自评结论；后端自评跳过时为 { overall: 'skipped', reason } */
  reflection?: LearningReflection
  /** A2A 接力：评分后要交给哪个下游 Agent（后台任务里派发，可能为 null） */
  next_agent?: string | null
  handoff_result?: unknown
}

export async function submitLearningWorkflowAnswers(runId: string, answers: LearningAnswerItem[]) {
  const { data: { data } } = await api.post<ApiResponse<LearningWorkflowSubmitResponse>>(
    `/learning-workflows/${runId}/submit`,
    { answers }
  )
  return data
}

/** 出题参数确认后的返回：题目就绪（status=waiting_answer）+ 题目本体 */
export type LearningWorkflowParamsResponse = {
  run_id: string
  status: string
  quiz: LearningQuiz
  question_count?: number | null
  knowledge_base_id?: number | null
  divergence?: number | null
  difficulty?: string | null
}

/**
 * 确认本轮出题参数并出题（第 23 轮"参数在对话里问"的第二步）。
 *
 * 参数没齐时后端只写了参数卡不出题，用户点"确认出题"后调这里；
 * 出题 run **原地**把那条消息从参数卡换成题目，所以前端只需回刷消息列表。
 */
export async function submitLearningWorkflowParams(
  runId: string,
  params: LearningWorkflowParamsSubmit
) {
  const { data: { data } } = await api.post<ApiResponse<LearningWorkflowParamsResponse>>(
    `/learning-workflows/${runId}/params`,
    params
  )
  return data
}

/** 运行状态查询的返回（题目本体在 `state.quiz` 里） */
export type LearningWorkflowDetailResponse = {
  run_id: string
  status: string
  state?: {
    quiz?: LearningQuiz
    practice_set_id?: number
    [key: string]: unknown
  }
}

/**
 * 查运行状态（第 25 轮轮询用）：`/params` 立即返回 `generating_quiz`，
 * 出题在后台跑；前端按固定间隔查这里，`waiting_answer` 就代表题目已就绪。
 */
export async function fetchLearningWorkflow(runId: string) {
  const { data: { data } } = await api.get<ApiResponse<LearningWorkflowDetailResponse>>(
    `/learning-workflows/${runId}`
  )
  return data
}
