<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { FullScreen, ScaleToOriginal } from '@element-plus/icons-vue'
import {
  getPracticeDetail,
  practiceStatusView,
  submitPracticeAnswers,
  type PracticeAttemptItem,
  type PracticeDetail,
  type PracticeListData,
} from '@/api/practice'
import type { LearningEvaluation } from '@/api/learningWorkflow'
import LearningQuizResult from '@/components/chat/LearningQuizResult.vue'
import QuizAnswerForm from '@/components/chat/QuizAnswerForm.vue'
import type { LearningQuizQuestion } from '@/types/chat'

/**
 * 练习历史：表格 + 逐题回看 + **对未作答的题补答**。
 * 回看直接复用答题结果组件（`LearningQuizResult`），把 attempts 适配成它的入参，
 * 这样"刚交卷看到的"和"事后回看的"长得一模一样。
 */
const props = defineProps<{
  data: PracticeListData | null
  loading: boolean
  knowledgeBaseNames?: Record<number, string>
}>()

const emit = defineEmits<{
  pageChange: [page: number]
  /** 补答完成：本场得分变了，外层要刷新练习列表与画像统计 */
  submitted: []
}>()

const detailVisible = ref(false)
const detailFullscreen = ref(false)
const detailLoading = ref(false)
const submitting = ref(false)
const detail = ref<PracticeDetail | null>(null)
// 补答表单的答案（只装未作答的那几题）
const pendingAnswers = ref<Record<string, unknown>>({})

const rows = computed(() => props.data?.practices ?? [])
const total = computed(() => props.data?.total ?? 0)

const formatTime = (ts: number) => new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })

const kbName = (knowledgeBaseId: number | null) => {
  if (!knowledgeBaseId) return '未归库'
  return props.knowledgeBaseNames?.[knowledgeBaseId] ?? `库 #${knowledgeBaseId}`
}

const openDetail = async (id: number) => {
  detailLoading.value = true
  detailVisible.value = true
  try {
    detail.value = await getPracticeDetail(id)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '练习详情加载失败')
    detailVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

// ---- 把 attempts 适配成 LearningQuizResult 的入参 ----
const questions = computed<LearningQuizQuestion[]>(
  () =>
    (detail.value?.attempts ?? []).map((attempt) => ({
      id: attempt.question_code,
      type: (['radio', 'checkbox', 'textarea'] as const).includes(
        attempt.question_type as 'radio' | 'checkbox' | 'textarea'
      )
        ? (attempt.question_type as 'radio' | 'checkbox' | 'textarea')
        : 'textarea',
      stem: attempt.stem,
      options: attempt.options ?? [],
      points: attempt.points,
    }))
)

/**
 * `attempts[].user_answer` 是后端原样返回的作答对象（`{"value": ...}`），
 * 展示与"是否作答"判断都要的是里面的 `value`——直接把它当答案用会渲染成 "[object Object]"。
 */
const answerValueOf = (attempt: PracticeAttemptItem): unknown => {
  const raw = attempt.user_answer
  if (raw && typeof raw === 'object' && 'value' in (raw as Record<string, unknown>)) {
    return (raw as Record<string, unknown>).value
  }
  return raw
}

const isEmptyValue = (value: unknown) => {
  if (Array.isArray(value)) return value.length === 0
  return String(value ?? '').trim().length === 0
}

const answers = computed<Record<string, unknown>>(() =>
  Object.fromEntries(
    (detail.value?.attempts ?? []).map((attempt) => [
      attempt.question_code,
      answerValueOf(attempt),
    ])
  )
)

/** 未作答的题：这些题不给参考评分，改由下方表单补答。 */
const pendingQuestions = computed(() =>
  questions.value.filter((question) => isEmptyValue(answers.value[question.id]))
)
const pendingIds = computed(() => pendingQuestions.value.map((question) => question.id))

/** 本场是否有评分结论：一场全是未作答的练习不该显示成一个"0 分"的评分卡。 */
const hasGraded = computed(() =>
  (detail.value?.attempts ?? []).some(
    (attempt) => attempt.score !== null || Boolean(attempt.analysis)
  )
)

const evaluation = computed<LearningEvaluation>(() => ({
  score: detail.value?.practice.total_score ?? 0,
  items: (detail.value?.attempts ?? []).map((attempt) => ({
    question_id: attempt.question_code,
    score: attempt.score ?? undefined,
    is_correct: attempt.is_correct,
    correct_answer: attempt.correct_answer ?? undefined,
    analysis: attempt.analysis ?? undefined,
  })),
}))

/** 自评反思的结论摘要：有 evidence 就列判据，否则回退到 overall/结论文案 */
const reflectionSummary = computed(() => {
  const reflection = detail.value?.reflection
  if (!reflection) return ''
  const overall = typeof reflection.overall === 'string' ? reflection.overall : ''
  const evidence = reflection.evidence
  if (evidence && typeof evidence === 'object') {
    const reason = typeof reflection.reason === 'string' ? reflection.reason : ''
    const parts: string[] = []
    if (reason) parts.push(reason)
    const verdicts = (
      ['weak_point_coverage', 'chapter_rotation', 'duplication', 'difficulty'] as const
    )
      .map((key) => {
        const item = reflection[key]
        if (!item || typeof item !== 'object') return ''
        const verdict = String((item as { verdict?: unknown }).verdict ?? '')
        return verdict ? `${key}: ${verdict}` : ''
      })
      .filter(Boolean)
    if (verdicts.length) parts.push(verdicts.join('；'))
    return parts.join('。')
  }
  const suggestions = Array.isArray(reflection.suggestions)
    ? reflection.suggestions.filter((x): x is string => typeof x === 'string').slice(0, 3)
    : []
  const suffix = suggestions.length ? ` 建议：${suggestions.join('；')}` : ''
  return `${overall || '已反思'}${suffix}`
})

const hasPendingInput = computed(
  () => Object.values(pendingAnswers.value).some((value) => !isEmptyValue(value))
)

// 打开/刷新详情后按题型初始化补答表单：多选为空数组，其余为空字符串
watch(
  pendingQuestions,
  (list) => {
    const next: Record<string, unknown> = {}
    list.forEach((question) => {
      next[question.id] = question.type === 'checkbox' ? [] : ''
    })
    pendingAnswers.value = next
  },
  { immediate: true }
)

const submitPending = async () => {
  if (!detail.value || submitting.value || !hasPendingInput.value) return
  submitting.value = true
  try {
    const payload = pendingQuestions.value
      .filter((question) => !isEmptyValue(pendingAnswers.value[question.id]))
      .map((question) => ({
        question_id: question.id,
        value: pendingAnswers.value[question.id],
      }))
    // 接口返回更新后的详情：直接换掉本地数据，得分 / 逐题点评立刻反映出来
    detail.value = await submitPracticeAnswers(detail.value.practice.id, payload)
    ElMessage.success('补答已评分')
    emit('submitted')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '补答提交失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="practice-history">
    <div class="practice-history__head">
      <span class="practice-history__title">练习历史</span>
      <span class="practice-history__hint">
        共 {{ total }} 场 · 点击「查看逐题」可回看每道题的得分与点评，未作答的题可以直接补答
      </span>
    </div>

    <el-table v-loading="loading" :data="rows" size="small" empty-text="还没有练习记录">
      <el-table-column prop="title" label="练习" min-width="180" show-overflow-tooltip />
      <el-table-column label="知识库" width="140">
        <template #default="{ row }">{{ kbName(row.knowledge_base_id) }}</template>
      </el-table-column>
      <el-table-column label="得分" width="80">
        <template #default="{ row }">
          <span :class="{ 'practice-history__score': row.total_score !== null }">
            {{ row.total_score ?? '—' }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="96">
        <template #default="{ row }">
          <el-tag size="small" :type="practiceStatusView(row.status).type" effect="light">
            {{ practiceStatusView(row.status).label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="question_count" label="题数" width="70" />
      <el-table-column label="时间" width="160">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="104" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="openDetail(row.id)">查看逐题</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="total > (data?.page_size ?? 10)" class="practice-history__pager">
      <el-pagination
        layout="prev, pager, next"
        :page-size="data?.page_size ?? 10"
        :total="total"
        :current-page="data?.page ?? 1"
        @current-change="(page: number) => emit('pageChange', page)" />
    </div>

    <el-dialog
      v-model="detailVisible"
      title="练习详情"
      width="780px"
      :fullscreen="detailFullscreen"
      append-to-body
      @closed="detailFullscreen = false">
      <template #header="{ titleId, titleClass }">
        <div class="practice-history__detail-bar">
          <span :id="titleId" :class="titleClass">练习详情</span>
          <el-button link type="primary" @click="detailFullscreen = !detailFullscreen">
            <el-icon><ScaleToOriginal v-if="detailFullscreen" /><FullScreen v-else /></el-icon>
            {{ detailFullscreen ? '退出全屏' : '全屏' }}
          </el-button>
        </div>
      </template>

      <div v-loading="detailLoading" class="practice-history__detail">
        <div v-if="detail" class="practice-history__detail-head">
          <span class="practice-history__detail-title">{{ detail.practice.title }}</span>
          <span class="practice-history__detail-meta">
            {{ kbName(detail.practice.knowledge_base_id) }} ·
            {{ formatTime(detail.practice.created_at) }}
          </span>
        </div>

        <!-- 总评与反思：第 27 轮起随练习详情落库（用户：练习记录里也要能看总评价和反思） -->
        <section
            v-if="detail && (detail.review || reflectionSummary)"
            class="practice-history__summary">
          <div v-if="detail.review" class="practice-history__summary-block">
            <span class="practice-history__summary-label">总评价</span>
            <p class="practice-history__summary-text">{{ detail.review }}</p>
          </div>
          <div v-if="reflectionSummary" class="practice-history__summary-block">
            <span class="practice-history__summary-label">自评反思</span>
            <p class="practice-history__summary-text">{{ reflectionSummary }}</p>
          </div>
        </section>

        <!-- 补答：未作答的题在这里作答，而不是照着一份"参考评分"看（用户 2026-10-19 报的问题） -->
        <section v-if="detail && pendingQuestions.length" class="practice-history__pending">
          <div class="practice-history__pending-head">
            <span class="practice-history__pending-title">
              还有 {{ pendingQuestions.length }} 道题没有作答
            </span>
            <span class="practice-history__pending-hint">
              填好后点「提交补答」：只对这几道题重新评分，并合并进本场练习
            </span>
          </div>
          <div class="practice-history__pending-body">
            <QuizAnswerForm
              :model-value="pendingAnswers"
              :questions="pendingQuestions"
              @update:model-value="pendingAnswers = $event" />
          </div>
        </section>

        <LearningQuizResult
          v-if="detail && hasGraded"
          :score="detail.practice.total_score ?? 0"
          review=""
          :weaknesses="[]"
          :evaluation="evaluation"
          :questions="questions"
          :answers="answers"
          :pending-ids="pendingIds" />
        <p v-else-if="detail" class="practice-history__empty">
          本场练习还没有评分记录：答完上面的题，提交后就会生成逐题得分与点评。
        </p>
        <el-empty v-else-if="!detailLoading" description="没有详情数据" />
      </div>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <el-button
          v-if="detail && pendingQuestions.length"
          type="primary"
          :loading="submitting"
          :disabled="!hasPendingInput"
          @click="submitPending">
          提交补答
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped lang="scss">
.practice-history__head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}

.practice-history__title {
  font-size: 15px;
  font-weight: 600;
  color: #30353e;
}

.practice-history__hint {
  font-size: 12px;
  color: #8a9099;
}

.practice-history__score {
  font-weight: 600;
  color: #30353e;
}

.practice-history__pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.practice-history__detail {
  min-height: 120px;
}

.practice-history__detail-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-right: 24px;
}

.practice-history__detail-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.practice-history__detail-title {
  font-size: 16px;
  font-weight: 600;
  color: #30353e;
}

.practice-history__detail-meta {
  font-size: 12px;
  color: #8a9099;
}

.practice-history__summary {
  margin-bottom: 16px;
  padding: 14px 16px;
  border: 1px solid #e6e8eb;
  border-radius: 10px;
  background: #fafbfc;
}

.practice-history__summary-block + .practice-history__summary-block {
  margin-top: 10px;
}

.practice-history__summary-label {
  display: block;
  margin-bottom: 4px;
  font-size: 12px;
  font-weight: 600;
  color: #8a9099;
}

.practice-history__summary-text {
  margin: 0;
  font-size: 14px;
  line-height: 1.7;
  color: #30353e;
  white-space: pre-wrap;
  word-break: break-word;
}

.practice-history__pending {
  margin-bottom: 16px;
  padding: 14px 16px;
  border: 1px solid #dcdfe6;
  border-radius: 10px;
  background: #f7f8fa;
}

.practice-history__pending-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
}

.practice-history__pending-title {
  font-size: 14px;
  font-weight: 600;
  color: #30353e;
}

.practice-history__pending-hint {
  font-size: 12px;
  color: #8a9099;
}

.practice-history__pending-body {
  max-height: 46vh;
  overflow-y: auto;
}

.practice-history__empty {
  margin: 0;
  font-size: 13px;
  line-height: 1.8;
  color: #8a9099;
}
</style>
