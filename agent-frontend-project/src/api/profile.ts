import api, { type ApiResponse } from './http'

/**
 * 学习画像接口：对应后端 app/routes/profile_routes.py。
 * 三样数据都是真的：练习次数/分数来自 practice_sets，掌握度来自 user_knowledge_mastery，
 * 弱点来自 user_profiles.weak_points（**没有"知识覆盖率"**，那个指标已明确不做）。
 */

export interface ProfileStats {
  practice_count: number
  completed_count: number
  average_score: number
  latest_score: number | null
  latest_practice_at: number | null
  /** 最近若干场分数（时间升序），画趋势用 */
  recent_scores: number[]
}

export type MasteryZone = '补课区' | '过渡区' | '优势区' | string

export interface MasteryItem {
  knowledge_base_id: number
  knowledge_base_name: string | null
  mastery_score: number
  zone: MasteryZone
  practice_count: number
  last_assessed_at: number | null
}

export interface WeakPointItem {
  point: string
  knowledge_base_id: number | null
  times_seen: number
  absent_streak: number
  improved: boolean
  archived: boolean
  axis: string
  first_seen: string | null
  last_seen: string | null
  last_checked_at: string | null
}

export interface WeakPointList {
  total: number
  active: number
  improved: number
  archived: number
  items: WeakPointItem[]
}

/** 画像页顶部统计（recent 控制趋势取多少场） */
export async function getProfileStats(recent = 10): Promise<ProfileStats> {
  const {
    data: { data },
  } = await api.get<ApiResponse<ProfileStats>>('/profile/stats', { params: { recent } })
  return data
}

/** 按知识库的掌握度与分区（掌握度降序） */
export async function getProfileMastery(): Promise<MasteryItem[]> {
  const {
    data: { data },
  } = await api.get<ApiResponse<{ items: MasteryItem[] }>>('/profile/mastery')
  return data.items
}

/** 弱点清单：活跃在前，已改进/已归档排后面 */
export async function getProfileWeakPoints(knowledgeBaseId?: number): Promise<WeakPointList> {
  const {
    data: { data },
  } = await api.get<ApiResponse<WeakPointList>>('/profile/weak-points', {
    params: knowledgeBaseId ? { knowledge_base_id: knowledgeBaseId } : undefined,
  })
  return data
}

/** 分区颜色：补课区红、过渡区黄、优势区绿 */
export function zoneTagType(zone: MasteryZone): '' | 'success' | 'warning' | 'danger' {
  if (zone === '优势区') return 'success'
  if (zone === '过渡区') return 'warning'
  if (zone === '补课区') return 'danger'
  return ''
}

export interface DueReviewItem {
  point: string
  knowledge_base_id: number | null
  times_seen: number
  /** SM-2 难度因子：越大间隔增长越快（下限 1.3，默认 2.5） */
  ease_factor: number
  /** 当前复习间隔（天） */
  interval_days: number
  /** 应复习日期（ISO，<= 今天即到期） */
  next_review: string
  last_seen: string | null
}

export interface DueReviewList {
  total: number
  /** 判定用的"今天"（ISO）：由后端给，避免前后端各算一次时区口径 */
  today: string
  items: DueReviewItem[]
}

/**
 * 今日到期复习（SM-2 调度，升级规划 §八 第 3 期）。
 * 只含**活跃**弱点：毕业条目退出归并后不再滚动计划，故不进此视图（口径见后端 review_service）。
 */
export async function getDueReviews(): Promise<DueReviewList> {
  const {
    data: { data },
  } = await api.get<ApiResponse<DueReviewList>>('/profile/reviews/due')
  return data
}
