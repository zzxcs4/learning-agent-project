<script setup lang="ts">
import { computed } from 'vue'
import type { LearningEvaluation, LearningEvaluationItem, LearningReflection } from '@/api/learningWorkflow'
import type { LearningQuizQuestion } from '@/types/chat'

/**
 * 答题结果结构化展示：总分 + 逐题（我的答案 / 正确答案 / 得分 / 点评）+ 薄弱点 + 总评
 * + **本轮出题质量（自评）**。
 *
 * 数据全部来自提交接口的返回值（`score` / `evaluation.items` / `weaknesses` / `review` /
 * `reflection`），所以不需要额外请求详情接口，也不会因为消息刷新而丢。
 *
 * `reflection` 是可选的：练习历史"回看"路径（`PracticeHistory` 把 attempts 适配进来）
 * 拿不到它——`workflow_runs` 没有可索引的 practice→run 关联，反思存在 `state_json` 里，
 * 硬取要扫 JSON。回看时这个折叠块不显示（已知缺口，不假装有数据）。
 */
const props = defineProps<{
  score: number
  review: string
  weaknesses: string[]
  evaluation: LearningEvaluation
  questions: LearningQuizQuestion[]
  answers: Record<string, unknown>
  reflection?: LearningReflection | null
  /** **当时没有作答**的题号：这些题不显示参考评分与参考答案/点评。
   *
   * 为什么需要它：评测节点会给每道题都打分（含没作答的题），于是那些题带着 0 分 + 参考答案
   * 落库；练习记录里如果照原样展示，用户看到的是"参考评分"，而不是"我还能补答"。
   * 补答入口在 `PracticeHistory`，这里只负责**不把未作答的题装成已评分**。
   */
  pendingIds?: string[]
}>()

type Verdict = 'correct' | 'partial' | 'wrong' | 'unknown'

const items = computed<LearningEvaluationItem[]>(() => props.evaluation?.items ?? [])

/** 未作答的题号集合：判定口径与后端补答一致（没有答题记录 / 答案值为空）。 */
const pendingSet = computed(() => new Set(props.pendingIds ?? []))
const isPending = (question: LearningQuizQuestion) => pendingSet.value.has(question.id)

/** 按题号把评分明细挂到题目上：items 的顺序不保证与题目一致 */
const itemByQuestion = computed<Record<string, LearningEvaluationItem>>(() => {
  const map: Record<string, LearningEvaluationItem> = {}
  items.value.forEach((item) => {
    if (item?.question_id) map[item.question_id] = item
  })
  return map
})

const verdictOf = (question: LearningQuizQuestion): Verdict => {
  const item = itemByQuestion.value[question.id]
  if (!item) return 'unknown'
  if (item.is_correct === true) return 'correct'
  const got = typeof item.score === 'number' ? item.score : 0
  const full = typeof question.points === 'number' ? question.points : 0
  if (full > 0 && got >= full) return 'correct'
  if (got > 0) return 'partial'
  return 'wrong'
}

const verdictView: Record<Verdict, { label: string; type: '' | 'success' | 'warning' | 'danger' | 'info' }> = {
  correct: { label: '正确', type: 'success' },
  partial: { label: '部分得分', type: 'warning' },
  wrong: { label: '需复习', type: 'danger' },
  unknown: { label: '未评分', type: 'info' },
}

/** 选项 value → 展示文本；主观题原样返回 */
const optionLabel = (question: LearningQuizQuestion, value: unknown): string => {
  const text = String(value ?? '').trim()
  if (!text) return ''
  const option = (question.options ?? []).find((row) => row.value === text)
  return option ? `${option.value}. ${option.label}` : text
}

/** 用户答案按题型格式化（多选拼成一个列表） */
const formatAnswer = (question: LearningQuizQuestion, value: unknown): string => {
  if (Array.isArray(value)) {
    const labels = value.map((row) => optionLabel(question, row)).filter(Boolean)
    return labels.length ? labels.join('；') : '未作答'
  }
  const text = optionLabel(question, value)
  return text || '未作答'
}

const correctCount = computed(() => props.questions.filter((row) => verdictOf(row) === 'correct').length)
const answeredCount = computed(
  () => props.questions.filter((row) => !isEmpty(props.answers[row.id])).length,
)
/** 真正有评分结论的题数（未作答的题不在分母里，否则"正确 1/5"会被未答题拉低）。 */
const gradedCount = computed(() => props.questions.filter((row) => !isPending(row)).length)

function isEmpty(value: unknown): boolean {
  if (Array.isArray(value)) return value.length === 0
  return String(value ?? '').trim().length === 0
}

/** 得分头部的评级：只用于本轮表现，不代表知识库掌握度 */
const grade = computed(() => {
  const score = props.score
  if (score >= 85) return { label: '表现很好', type: 'success' as const }
  if (score >= 70) return { label: '基本掌握', type: '' as const }
  if (score >= 50) return { label: '还需要练', type: 'warning' as const }
  return { label: '建议先补基础', type: 'danger' as const }
})

// --------------------------------------------------------------------------
// 反思（升级规划 §7.2）：对**本轮出题质量**的自评，不是对用户表现的评分。
// verdict 的词来自后端白名单（reflection_service），这里只做展示映射。
// --------------------------------------------------------------------------
type TagType = '' | 'success' | 'warning' | 'danger' | 'info'

const VERDICT_VIEW: Record<string, { label: string; type: TagType }> = {
  good: { label: '覆盖已达标', type: 'success' },
  partial: { label: '部分覆盖', type: 'warning' },
  missed: { label: '没覆盖薄弱点', type: 'danger' },
  diverse: { label: '章节有轮换', type: 'success' },
  narrow: { label: '章节偏窄', type: 'warning' },
  fresh: { label: '与历史不重复', type: 'success' },
  repetitive: { label: '与历史重复', type: 'warning' },
  appropriate: { label: '难度合适', type: 'success' },
  too_easy: { label: '偏易', type: 'warning' },
  too_hard: { label: '偏难', type: 'warning' },
  unknown: { label: '证据不足', type: 'info' },
}

const reflectionVerdictView = (verdict?: string) =>
  VERDICT_VIEW[verdict ?? 'unknown'] ?? { label: verdict || '—', type: 'info' as TagType }

const reflection = computed(() => props.reflection ?? null)
/** 老 run / 自评跳过时可能只有 reason，也要能展示 */
const reflectionVisible = computed(() => Boolean(reflection.value && Object.keys(reflection.value).length))

const reflectionOverall = computed(() => {
  const overall = reflection.value?.overall ?? 'unknown'
  if (overall === 'pass') return { label: '通过', type: 'success' as TagType }
  if (overall === 'warn') return { label: '有改进项', type: 'warning' as TagType }
  if (overall === 'skipped') return { label: '本次未自评', type: 'info' as TagType }
  return { label: '证据不足', type: 'info' as TagType }
})

const reflectionNote = computed(
  () =>
    reflection.value?.reason ||
    '这是对"本轮出题"的自评（不是对你表现的评分）：聚焦组查是否覆盖你的薄弱点，发散组查章节轮换、是否与历史重复、难度是否合适。',
)

const reflectionRows = computed(() => {
  const data = reflection.value
  if (!data) return []
  const rows: { label: string; text: string; type: TagType; detail: string }[] = []
  const push = (label: string, section: { verdict?: string } | undefined, detail: () => string) => {
    if (!section) return
    const view = reflectionVerdictView(section.verdict)
    rows.push({ label, text: view.label, type: view.type, detail: detail() })
  }
  push('薄弱点覆盖', data.weak_point_coverage, () => {
    const covered = data.weak_point_coverage?.covered ?? []
    const missed = data.weak_point_coverage?.missed ?? []
    return [
      covered.length ? `覆盖 ${covered.length} 条` : '',
      missed.length ? `漏掉：${missed.join('、')}` : '',
    ]
      .filter(Boolean)
      .join(' · ')
  })
  push('章节轮换', data.chapter_rotation, () => (data.chapter_rotation?.chapters ?? []).join('、'))
  push('与历史重复', data.duplication, () => (data.duplication?.duplicates ?? []).join('、'))
  push('难度', data.difficulty, () => data.difficulty?.evidence ?? '')
  return rows
})
</script>

<template>
  <div class="quiz-result">
    <div class="quiz-result__summary">
      <div class="quiz-result__score">
        <span class="quiz-result__score-value">{{ Math.round(score) }}</span>
        <span class="quiz-result__score-unit">分</span>
      </div>
      <div class="quiz-result__summary-meta">
        <el-tag :type="grade.type" effect="light">{{ grade.label }}</el-tag>
        <span class="quiz-result__stat">
          正确 {{ correctCount }}/{{ gradedCount }} 题 · 已作答 {{ answeredCount }}/{{ questions.length }} 题
        </span>
      </div>
    </div>

    <div v-if="weaknesses.length" class="quiz-result__weak">
      <span class="quiz-result__section-title">本轮薄弱点</span>
      <el-tag v-for="point in weaknesses" :key="point" type="warning" effect="plain" size="small">
        {{ point }}
      </el-tag>
    </div>

    <el-collapse class="quiz-result__list">
      <el-collapse-item v-for="(question, index) in questions" :key="question.id" :name="question.id">
        <template #title>
          <div class="quiz-result__item-title">
            <span class="quiz-result__stem">{{ index + 1 }}. {{ question.stem }}</span>
            <span class="quiz-result__item-tags">
              <el-tag
                :type="isPending(question) ? 'info' : verdictView[verdictOf(question)].type"
                size="small"
                effect="dark">
                {{ isPending(question) ? '待作答' : verdictView[verdictOf(question)].label }}
              </el-tag>
              <span class="quiz-result__item-score">
                {{ isPending(question) ? '—' : (itemByQuestion[question.id]?.score ?? 0) }}/{{
                  question.points ?? 0
                }}
                分
              </span>
            </span>
          </div>
        </template>

        <!-- 当时没作答：不给参考评分与点评，只说明它还没算分（补答入口在练习记录里） -->
        <p v-if="isPending(question)" class="quiz-result__pending">
          这道题当时没有作答，因此不计分，也不显示参考答案与点评。
        </p>
        <template v-else>
          <div class="quiz-result__answer-row">
            <span class="quiz-result__label">我的答案</span>
            <span class="quiz-result__answer">{{ formatAnswer(question, answers[question.id]) }}</span>
          </div>
          <div class="quiz-result__answer-row">
            <span class="quiz-result__label">正确答案</span>
            <span class="quiz-result__answer">
              {{ itemByQuestion[question.id]?.correct_answer || '—' }}
            </span>
          </div>
          <div v-if="itemByQuestion[question.id]?.analysis" class="quiz-result__analysis">
            <span class="quiz-result__label">点评</span>
            <span>{{ itemByQuestion[question.id]?.analysis }}</span>
          </div>
        </template>
      </el-collapse-item>
    </el-collapse>

    <div v-if="review" class="quiz-result__review">
      <span class="quiz-result__section-title">总评与建议</span>
      <p>{{ review }}</p>
    </div>

    <!-- 出题质量自评：默认折叠，它是元信息，不该抢"你考了多少分"的注意力 -->
    <el-collapse v-if="reflectionVisible" class="quiz-result__reflection">
      <el-collapse-item name="reflection">
        <template #title>
          <div class="quiz-result__reflection-title">
            <span class="quiz-result__section-title">本轮出题质量（自评）</span>
            <el-tag :type="reflectionOverall.type" size="small" effect="light">
              {{ reflectionOverall.label }}
            </el-tag>
          </div>
        </template>

        <p class="quiz-result__reflection-note">{{ reflectionNote }}</p>

        <div v-for="row in reflectionRows" :key="row.label" class="quiz-result__reflection-row">
          <span class="quiz-result__reflection-label">{{ row.label }}</span>
          <el-tag :type="row.type" size="small" effect="plain">{{ row.text }}</el-tag>
          <span v-if="row.detail" class="quiz-result__reflection-detail">{{ row.detail }}</span>
        </div>

        <div v-if="reflection?.suggestions?.length" class="quiz-result__reflection-suggest">
          <span class="quiz-result__label">下次这样出题</span>
          <ul>
            <li v-for="(tip, index) in reflection.suggestions" :key="index">{{ tip }}</li>
          </ul>
        </div>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<style scoped lang="scss">
.quiz-result__summary {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 16px 18px;
  border-radius: 12px;
  background: #f7f8fa;
}

.quiz-result__score {
  display: flex;
  align-items: baseline;
  color: #30353e;
}

.quiz-result__score-value {
  font-size: 40px;
  font-weight: 700;
  line-height: 1;
}

.quiz-result__score-unit {
  margin-left: 4px;
  font-size: 14px;
  color: #8a9099;
}

.quiz-result__summary-meta {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.quiz-result__stat {
  font-size: 13px;
  color: #6b7280;
}

.quiz-result__weak {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
}

.quiz-result__section-title {
  font-size: 14px;
  font-weight: 600;
  color: #30353e;
  margin-right: 4px;
}

.quiz-result__list {
  margin-top: 12px;
}

.quiz-result__item-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-right: 8px;
}

.quiz-result__stem {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  color: #30353e;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.quiz-result__item-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.quiz-result__item-score {
  font-size: 13px;
  color: #6b7280;
}

.quiz-result__answer-row {
  display: flex;
  gap: 10px;
  margin-bottom: 6px;
  font-size: 13px;
  line-height: 1.7;
}

.quiz-result__label {
  flex-shrink: 0;
  width: 64px;
  color: #8a9099;
}

.quiz-result__answer {
  color: #30353e;
  word-break: break-word;
}

.quiz-result__analysis {
  display: flex;
  gap: 10px;
  margin-top: 6px;
  font-size: 13px;
  line-height: 1.7;
  color: #434952;
}

.quiz-result__pending {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: #8a9099;
}

.quiz-result__review {
  margin-top: 16px;
  padding: 14px 16px;
  border-radius: 10px;
  background: #f7f8fa;

  p {
    margin: 8px 0 0;
    font-size: 13px;
    line-height: 1.8;
    color: #434952;
    white-space: pre-wrap;
  }
}

.quiz-result__reflection {
  margin-top: 12px;
}

.quiz-result__reflection-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.quiz-result__reflection-note {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.7;
  color: #8a9099;
}

.quiz-result__reflection-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 0;
  font-size: 13px;
}

.quiz-result__reflection-label {
  flex-shrink: 0;
  width: 84px;
  color: #6b7280;
}

.quiz-result__reflection-detail {
  color: #434952;
  word-break: break-word;
}

.quiz-result__reflection-suggest {
  display: flex;
  gap: 10px;
  margin-top: 10px;
  font-size: 13px;
  color: #434952;

  ul {
    margin: 0;
    padding-left: 18px;
    line-height: 1.8;
  }
}
</style>
