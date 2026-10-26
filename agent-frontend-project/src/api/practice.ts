import api, { type ApiResponse } from './http'

/**
 * 练习历史与单场详情：对应后端 app/routes/practice_routes.py。
 * 详情里的 `attempts` 就是逐题评分明细，可复用答题结果组件展示。
 */

export interface PracticeHistoryItem {
  id: number
  title: string
  knowledge_base_id: number | null
  status: string
  total_score: number | null
  question_count: number
  /** 关联的工作流 run_id（第 27 轮起落库）：归档定位某一场练习用 */
  run_id?: string | null
  created_at: number
}

export interface PracticeListData {
  page: number
  page_size: number
  total: number
  has_more: boolean
  practices: PracticeHistoryItem[]
}

export interface PracticeAttemptItem {
  question_id: number
  question_code: string
  question_type: string
  stem: string
  focus_area: string | null
  /** 选项（单选/多选有值），回看时把答案 value 还原成选项文本 */
  options: { value: string; label: string }[] | null
  points: number
  user_answer: unknown
  score: number | null
  is_correct: boolean | null
  /** 参考答案（评测时回写到 questions.answer） */
  correct_answer: string | null
  analysis: string | null
}

export interface PracticeDetail {
  practice: PracticeHistoryItem
  attempts: PracticeAttemptItem[]
  /** 整体总评（第 27 轮起从对应 run 的 state 带出，练习记录回看用） */
  review?: string | null
  /** 自评反思结论 */
  reflection?: Record<string, unknown> | null
}

export interface PracticeListQuery {
  page?: number
  page_size?: number
  knowledge_base_id?: number
}

/** 分页获取练习历史（最新在前） */
export async function listPractices(query: PracticeListQuery = {}): Promise<PracticeListData> {
  const {
    data: { data },
  } = await api.get<ApiResponse<PracticeListData>>('/practice', {
    params: { page: 1, page_size: 10, ...query },
  })
  return data
}

/** 单场练习详情：套题信息 + 逐题作答与评分 */
export async function getPracticeDetail(practiceSetId: number): Promise<PracticeDetail> {
  const {
    data: { data },
  } = await api.get<ApiResponse<PracticeDetail>>(`/practice/${practiceSetId}`)
  return data
}

/**
 * 给练习里**未作答**的题目补答并重新评分：返回更新后的详情。
 * 已作答的题不会被覆盖（要改答案就是一次新的练习）。
 */
export async function submitPracticeAnswers(
  practiceSetId: number,
  answers: { question_id: string; value: unknown }[]
): Promise<PracticeDetail> {
  const {
    data: { data },
  } = await api.post<ApiResponse<PracticeDetail>>(`/practice/${practiceSetId}/answers`, { answers })
  return data
}

/** 练习状态文案 */
export function practiceStatusView(status: string): { label: string; type: '' | 'success' | 'warning' | 'info' } {
  if (status === 'completed') return { label: '已评分', type: 'success' }
  if (status === 'waiting_answer') return { label: '待作答', type: 'warning' }
  return { label: status, type: 'info' }
}
