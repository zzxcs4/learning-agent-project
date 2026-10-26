<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElLoading, ElMessage } from 'element-plus'
import { FullScreen, ScaleToOriginal } from '@element-plus/icons-vue'
import {
  fetchLearningWorkflow,
  submitLearningWorkflowAnswers,
  type LearningEvaluation,
  type LearningReflection,
  type LearningWorkflowSubmitResponse,
} from '@/api/learningWorkflow'
import LearningQuizResult from '@/components/chat/LearningQuizResult.vue'
import QuizAnswerForm from '@/components/chat/QuizAnswerForm.vue'
import type { LearningQuizPayload, UiMessage } from '@/types/chat'

const props = defineProps<{
  message: UiMessage
  /** 本页会话**刚出的题**：挂载后直接弹出来。从历史/切会话加载的消息传 false。 */
  autoOpen?: boolean
}>()

const emit = defineEmits<{
  submitted: []
}>()

// 控制弹窗、提交状态和用户答案缓存
const dialogVisible = ref(false)
const resultVisible = ref(false)
// 全屏开关：题目长、简答题多时，720px 的弹窗里做卷很难受（用户 2026-10-19 要求）
const answerFullscreen = ref(false)
const resultFullscreen = ref(false)
const submitting = ref(false)
const answers = ref<Record<string, unknown>>({})
// 提交成功后把结果留在组件里：同一次页面会话内可以再次查看"逐题结果"
const result = ref<LearningWorkflowSubmitResponse | null>(null)
const submittedAnswers = ref<Record<string, unknown>>({})

/** 评分在后台跑（第 25 轮）：轮询运行状态，`completed` 后从 state 取分数/点评 */
const grading = ref(false)
const gradingHint = ref('')
const POLL_INTERVAL_MS = 2500
const POLL_MAX_TRIES = 120
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollTries = 0
let stopped = false

// 从消息载荷中提取测评数据，runId 兜底取 requestId 前缀
const payload = computed(() => props.message.responsePayload as LearningQuizPayload | undefined)
const quiz = computed(() => payload.value?.quiz)
const questions = computed(() => quiz.value?.questions ?? [])
const runId = computed(() => payload.value?.run_id || props.message.requestId?.split(':')[0] || '')
const hasResult = computed(() => result.value !== null)

// 入口文案：评分中 > 已出结果 > 去答题
const entryLabel = computed(() => {
  if (grading.value) return '评分生成中…'
  if (hasResult.value) return '查看评分结果'
  return '查看并填写测评题目'
})

const openEntry = () => {
  if (grading.value) {
    ElMessage.info('评分还在生成，稍等一下就能看到结果')
    return
  }
  if (hasResult.value) {
    resultVisible.value = true
    return
  }
  dialogVisible.value = true
}

const isEmptyAnswer = (value: unknown) => {
  if (Array.isArray(value)) return value.length === 0
  return String(value ?? '').trim().length === 0
}

// 根据题型初始化答案：多选为空数组，其余为空字符串
const initAnswers = () => {
  const next: Record<string, unknown> = {}
  questions.value.forEach((question) => {
    next[question.id] = question.type === 'checkbox' ? [] : ''
  })
  answers.value = next
}

// 提交前检查所有必答题是否已填写
const validateAnswers = () => {
  const missing = questions.value.find((question) => question.required !== false && isEmptyAnswer(answers.value[question.id]))
  if (missing) {
    ElMessage.warning('请先完成所有必答题')
    return false
  }
  return true
}

/** 关闭结果弹窗后再通知父组件刷新消息——否则父组件重载会把本组件卸载，结果弹窗一闪就没 */
let notified = false
const notifySubmitted = () => {
  if (notified) return
  notified = true
  emit('submitted')
}

const stopPolling = () => {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

/** 把运行状态拼成结果视图要的形状（字段与 `/submit` 的同步响应一致） */
const toSubmitResponse = (
  detail: { run_id?: string; status?: string; state?: Record<string, unknown> }
): LearningWorkflowSubmitResponse => {
  const state = (detail.state ?? {}) as Record<string, unknown>
  const envelope = (state.a2a_envelope ?? {}) as { next_agent?: string }
  return {
    run_id: String(detail.run_id ?? runId.value),
    status: String(detail.status ?? 'completed'),
    score: Number(state.score ?? 0),
    evaluation: (state.evaluation ?? {}) as LearningEvaluation,
    weaknesses: (state.weaknesses ?? []) as string[],
    review: String(state.review ?? ''),
    reflection: state.reflection as LearningReflection | undefined,
    next_agent: envelope.next_agent,
  }
}

/** 恢复"我的答案"：刷新页面回来时本地表单是空的，答案在运行状态里（`state.answers`） */
const restoreSubmittedAnswers = (detail: { state?: Record<string, unknown> }) => {
  const hasLocal = Object.values(answers.value).some((value) => !isEmptyAnswer(value))
  if (hasLocal) return false
  const stored = (detail.state ?? {}).answers
  if (!Array.isArray(stored) || stored.length === 0) return false
  const restored: Record<string, unknown> = {}
  stored.forEach((item) => {
    const row = item as { question_id?: string; value?: unknown }
    if (row.question_id) restored[String(row.question_id)] = row.value
  })
  submittedAnswers.value = restored
  return true
}

/**
 * 从**消息载荷**恢复已评出结果的卡片（用户 2026-10-19：结果关掉后无法再看）。
 *
 * 第 27 轮起后端落库时把评分结果一并写进 `response_payload.status=completed`，
 * 于是刷新页面/从历史回来，这条出题消息**本身**就带着结果，直接渲染结果视图，
 * 而不是回到一张"已交卷的答题表单"（再交会撞 400）。这是唯一真相：`result` 在内存里
 * 活一次就没了，必须能从持久化载荷里重建。
 */
const restoreResultFromPayload = () => {
  if (result.value) return
  if (!payload.value || payload.value.status !== 'completed') return
  const p = payload.value
  if (p.score === undefined && p.review === undefined) return
  result.value = {
    run_id: p.run_id || String(runId.value),
    status: 'completed',
    score: Number(p.score ?? 0),
    evaluation: (p.evaluation ?? {}) as LearningEvaluation,
    weaknesses: (p.weaknesses ?? []) as string[],
    review: String(p.review ?? ''),
    reflection: p.reflection as LearningReflection | undefined,
    next_agent: undefined,
  }
  // 恢复"我的答案"
  const restored: Record<string, unknown> = {}
  ;(p.answers ?? []).forEach((item) => {
    if (item.question_id) restored[String(item.question_id)] = item.value
  })
  submittedAnswers.value = restored
}

/** 轮询到评分完成：completed → 直接展示结果；failed/超时 → 给口径并让父级回刷 */
const pollUntilGraded = async () => {
  pollTries = 0
  const tick = async () => {
    if (stopped) return
    pollTries += 1
    try {
      const detail = await fetchLearningWorkflow(runId.value)
      const status = String(detail?.status || '')
      if (status === 'completed') {
        stopPolling()
        grading.value = false
        restoreSubmittedAnswers(detail)
        result.value = toSubmitResponse(detail)
        dialogVisible.value = false
        resultVisible.value = true
        ElMessage.success('评分已生成')
        return
      }
      if (status === 'failed') {
        stopPolling()
        grading.value = false
        gradingHint.value = '评分失败，可以重新出一套题再练'
        ElMessage.error('评分失败')
        notifySubmitted()
        return
      }
    } catch {
      // 单次轮询失败不算失败（网络抖动/后端重启）：继续按间隔重试
    }
    if (pollTries >= POLL_MAX_TRIES) {
      stopPolling()
      grading.value = false
      gradingHint.value = '评分还在生成，稍后回到这个会话就能看到结果'
      ElMessage.info(gradingHint.value)
      notifySubmitted()
      return
    }
    pollTimer = setTimeout(() => void tick(), POLL_INTERVAL_MS)
  }
  await tick()
}

const submitAnswers = async () => {
  if (!runId.value || submitting.value) return
  if (!validateAnswers()) return

  // 防止重复提交，并在评分生成期间展示全屏加载
  submitting.value = true
  const loading = ElLoading.service({
    lock: true,
    text: '正在提交答案并生成评分，请稍候...',
    background: 'rgba(255, 255, 255, 0.6)',
  })
  try {
    const submittedAnswersPayload = questions.value.map((question) => ({
      question_id: question.id,
      value: answers.value[question.id],
    }))

    const response = await submitLearningWorkflowAnswers(runId.value, submittedAnswersPayload)
    if (response.status === 'grading') {
      // 后端立即返回、评分在后台跑：轮询到完成再展示结果（同步等会被 30s 超时掐断）
      grading.value = true
      gradingHint.value = ''
      loading.setText('正在生成评分与点评，通常 20–40 秒...')
      await pollUntilGraded()
      return
    }
    // 兼容同步返回（老后端/评分已经就绪）
    result.value = response
    submittedAnswers.value = { ...answers.value }
    dialogVisible.value = false
    resultVisible.value = true
    ElMessage.success('评分已生成')
  } catch (error) {
    const message = error instanceof Error ? error.message : '提交失败，请稍后重试'
    ElMessage.error(message)
  } finally {
    loading.close()
    submitting.value = false
  }
}

/** 关闭答题弹窗时退出全屏：否则下次打开会莫名其妙占满整个屏幕。 */
const onAnswerDialogClosed = () => {
  answerFullscreen.value = false
}

/** 结果弹窗关掉后再刷新消息（顺手复位全屏状态）。 */
const onResultDialogClosed = () => {
  resultFullscreen.value = false
  notifySubmitted()
}

onMounted(() => {
  initAnswers()
  // 已评出结果的卡片：从消息载荷恢复结果视图（刷新/历史回来都能再看）
  restoreResultFromPayload()
  // 刷新页面回来时这条消息还在"正在评分"：接着轮询（不重复提交、也不会卡在一张已交的卷上）
  if (payload.value?.status === 'grading') {
    grading.value = true
    void pollUntilGraded()
    return
  }
  // 只对本页会话刚出的题自动弹出：历史加载/切会话回来的消息不弹，
  // 否则一个会话里有几套题就会同时弹出几个答题弹窗。
  if (props.autoOpen && !result.value) {
    dialogVisible.value = true
  }
})

onBeforeUnmount(() => {
  stopped = true
  stopPolling()
})
</script>

<template>
  <div class="learning-quiz">
    <button type="button" class="learning-quiz__entry" @click="openEntry">
      {{ entryLabel }}
    </button>
    <!-- 评分在后台生成时的口径（轮询超时/失败都写这里，用户不用猜） -->
    <p v-if="gradingHint" class="learning-quiz__hint">{{ gradingHint }}</p>

    <el-dialog
      v-model="dialogVisible"
      :title="quiz?.title || '学习测评'"
      width="720px"
      :fullscreen="answerFullscreen"
      append-to-body
      :close-on-click-modal="!submitting"
      :close-on-press-escape="!submitting"
      @closed="onAnswerDialogClosed">
      <!-- 自定义头部：题目长的时候要能一键全屏（720px 里做简答题太窄） -->
      <template #header="{ titleId, titleClass }">
        <div class="learning-quiz__header">
          <span :id="titleId" :class="titleClass">{{ quiz?.title || '学习测评' }}</span>
          <el-button link type="primary" @click="answerFullscreen = !answerFullscreen">
            <el-icon><ScaleToOriginal v-if="answerFullscreen" /><FullScreen v-else /></el-icon>
            {{ answerFullscreen ? '退出全屏' : '全屏' }}
          </el-button>
        </div>
      </template>

      <p v-if="quiz?.description" class="learning-quiz__desc">{{ quiz.description }}</p>

      <div class="learning-quiz__body" :class="{ 'learning-quiz__body--fullscreen': answerFullscreen }">
        <QuizAnswerForm v-model="answers" :questions="questions" />
      </div>

      <template #footer>
        <el-button :disabled="submitting || grading" @click="dialogVisible = false">稍后填写</el-button>
        <el-button type="primary" :loading="submitting || grading" :disabled="grading" @click="submitAnswers">
          {{ grading ? '正在评分…' : '提交答案' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 评分结果：结构化展示逐题得分 / 我的答案 / 正确答案 / 点评 -->
    <el-dialog
      v-model="resultVisible"
      title="本次测评结果"
      width="760px"
      :fullscreen="resultFullscreen"
      append-to-body
      @closed="onResultDialogClosed">
      <template #header="{ titleId, titleClass }">
        <div class="learning-quiz__header">
          <span :id="titleId" :class="titleClass">本次测评结果</span>
          <el-button link type="primary" @click="resultFullscreen = !resultFullscreen">
            <el-icon><ScaleToOriginal v-if="resultFullscreen" /><FullScreen v-else /></el-icon>
            {{ resultFullscreen ? '退出全屏' : '全屏' }}
          </el-button>
        </div>
      </template>

      <LearningQuizResult
        v-if="result"
        :score="result.score"
        :review="result.review"
        :weaknesses="result.weaknesses"
        :evaluation="result.evaluation"
        :reflection="result.reflection"
        :questions="questions"
        :answers="submittedAnswers" />
      <template #footer>
        <el-button type="primary" @click="resultVisible = false">知道了</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.learning-quiz {
  margin-top: 12px;
}

/* 评分在后台生成时的口径（超时/失败提示） */
.learning-quiz__hint {
  margin: 6px 0 0;
  font-size: 12px;
  color: #596272;
}

.learning-quiz__entry {
  border: none;
  border-radius: 8px;
  padding: 8px 14px;
  background: #000000;
  color: #ffffff;
  cursor: pointer;
  font-size: 14px;

  &:hover {
    background: #383838;
  }
}

.learning-quiz__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-right: 24px;
}

.learning-quiz__desc {
  margin: 0 0 16px;
  color: #606266;
  line-height: 1.6;
}

.learning-quiz__body {
  max-height: 58vh;
  overflow-y: auto;
}

.learning-quiz__body--fullscreen {
  // 全屏下把"卷面"尽量铺开：留给头部 + 底部按钮约 220px
  max-height: calc(100vh - 220px);
}
</style>
