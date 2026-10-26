<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchLearningWorkflow, submitLearningWorkflowParams } from '@/api/learningWorkflow'
import type {
    LearningQuizParamsField,
    LearningQuizParamsSpec,
    LearningWorkflowParamsSubmit,
} from '@/types/chat'

/**
 * 出题参数卡（第 23 轮）：出题参数不再由发送前的表单决定，而是"在对话里问"。
 *
 * 后端 `quiz_params_service` 已经算好了**问什么**：`fields` 是全量字段，
 * `missing` 是需要用户拍板的 key。所以这里只做**泛型渲染**，不写死任何业务口径
 * （题数 5–20、发散度五档文案、难度三档、知识库候选都从 spec 里读）——
 * 口径只有一处真相，改后端就够，前端不会静默走偏。
 *
 * `resolved=true` 的字段（例如"只有一个知识库、已自动使用"）不再问，只作说明展示。
 *
 * **第 25 轮：确认后不等响应里的题目**。真模型出题要 30–60 秒，以前同步等会被前端
 * 30 秒超时掐断（用户看到"报超时"，其实题已经出好了）。现在 `/params` 立即返回
 * `status=generating_quiz`，这里轮询运行状态；完成时出题 run 已经把这条消息**原地**
 * 换成题目，所以只需 emit `confirmed` 让父级回刷消息列表。
 * 刷新页面回来时消息状态仍是 `generating_quiz`，`onMounted` 会自动接着轮询。
 */
const props = defineProps<{
    runId: string
    spec: LearningQuizParamsSpec
    /** 消息里的 payload 状态：刷新回来时靠它判断"要不要接着轮询" */
    status?: string
}>()

const emit = defineEmits<{
    /** 参数已确认、题目已生成（父级据此回刷消息 → 换成答题弹窗） */
    confirmed: [runId: string]
}>()

const submitting = ref(false)

/** 正在出题（后端生成中）：显示进度、隐藏表单、开轮询 */
const generating = ref(false)
/** 生成超时/失败时给用户的口径（题目可能稍后才落进会话） */
const pendingHint = ref('')

/** 轮询间隔 2.5s（真模型 30–60s 完成，够快也不打后端）；上限 5 分钟，到点就不再转圈 */
const POLL_INTERVAL_MS = 2500
const POLL_MAX_TRIES = 120
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollTries = 0
let stopped = false

/** 需要用户拍板的字段（后端 missing 是权威口径；退化时用 resolved=false 兜底） */
const askedFields = computed<LearningQuizParamsField[]>(() => {
    const missing = props.spec.missing ?? []
    const byMissing = props.spec.fields.filter((field) => missing.includes(field.key))
    if (byMissing.length) return byMissing
    return props.spec.fields.filter((field) => field.resolved !== true)
})

/** 后端已有权威值的字段：只展示，不再问 */
const settledFields = computed<LearningQuizParamsField[]>(() =>
    props.spec.fields.filter((field) => field.resolved === true)
)

// 表单值：初值优先用后端给的 value，其次 default（= "什么都不改直接确认"也能出题）
const form = reactive<Record<string, unknown>>({})

const initialValue = (field: LearningQuizParamsField): unknown => {
    if (field.value !== null && field.value !== undefined) return field.value
    return field.default ?? undefined
}

watch(
    () => props.spec,
    (spec) => {
        for (const field of spec.fields ?? []) form[field.key] = initialValue(field)
    },
    { immediate: true }
)

const setField = (key: string, value: unknown) => {
    form[key] = value
}

/** 提交体只带后端认得的四个 key，且跳过空值（空值走后端默认，不会被误当成 0） */
const buildSubmit = (): LearningWorkflowParamsSubmit => {
    const payload: LearningWorkflowParamsSubmit = {}
    for (const field of askedFields.value) {
        const value = form[field.key]
        if (value === null || value === undefined || value === '') continue
        const key = field.key as keyof LearningWorkflowParamsSubmit
        ;(payload as Record<string, unknown>)[key] = value
    }
    return payload
}

const stopPolling = () => {
    if (pollTimer) {
        clearTimeout(pollTimer)
        pollTimer = null
    }
}

/** 轮询运行状态直到题目就绪；就绪/failed/超时都会回刷消息列表 */
const pollUntilReady = async () => {
    pollTries = 0
    const tick = async () => {
        if (stopped) return
        pollTries += 1
        try {
            const detail = await fetchLearningWorkflow(props.runId)
            const status = String(detail?.status || '')
            if (status === 'waiting_answer') {
                stopPolling()
                generating.value = false
                const quiz = (detail?.state as { quiz?: { questions?: unknown[] } } | undefined)?.quiz
                const count = quiz?.questions?.length ?? 0
                ElMessage.success(count ? `题目已生成（${count} 题）` : '题目已生成')
                emit('confirmed', props.runId)
                return
            }
            if (status === 'failed') {
                stopPolling()
                generating.value = false
                pendingHint.value = '出题失败，可以再说一次「出题」重试'
                ElMessage.error('出题失败')
                emit('confirmed', props.runId)
                return
            }
        } catch {
            // 单次轮询失败不算失败（网络抖动/后端重启）：继续按间隔重试
        }
        if (pollTries >= POLL_MAX_TRIES) {
            stopPolling()
            generating.value = false
            pendingHint.value = '题目还在生成，稍后回到这个会话就能看到'
            emit('confirmed', props.runId)
            return
        }
        pollTimer = setTimeout(() => void tick(), POLL_INTERVAL_MS)
    }
    await tick()
}

const confirm = async () => {
    submitting.value = true
    pendingHint.value = ''
    try {
        const result = await submitLearningWorkflowParams(props.runId, buildSubmit())
        const count = result?.quiz?.questions?.length ?? 0
        if (count) {
            // 兼容同步返回（老后端/参数已全）：题目当场就有
            ElMessage.success(`题目已生成（${count} 题）`)
            emit('confirmed', props.runId)
            return
        }
        generating.value = true
        ElMessage.info('正在出题，通常 30–60 秒，完成后题目会自动出现')
        await pollUntilReady()
    } catch (error) {
        generating.value = false
        ElMessage.error(error instanceof Error ? error.message : '确认出题参数失败')
    } finally {
        submitting.value = false
    }
}

// 刷新页面回来：消息状态还是"正在出题"就接着轮询（不重复提交，也不会卡死在旧卡上）
onMounted(() => {
    if (props.status === 'generating_quiz') {
        generating.value = true
        void pollUntilReady()
    }
})

watch(
    () => props.status,
    (status) => {
        if (status === 'generating_quiz' && !generating.value) {
            generating.value = true
            void pollUntilReady()
        }
    }
)

onBeforeUnmount(() => {
    stopped = true
    stopPolling()
})

/** 知识库候选的展示文案：带上文档数；"不使用知识库"那一项没有 resource_count，只显示标签 */
const optionLabel = (field: LearningQuizParamsField, value: string | number) => {
    const option = (field.options ?? []).find((item) => item.value === value)
    if (!option) return String(value)
    if (field.key !== 'knowledge_base_id') return option.label
    const count = option.resource_count
    if (count === undefined) return option.label
    return count ? `${option.label}（${count} 份资料）` : `${option.label}（暂无资料）`
}
</script>

<template>
    <div class="quiz-params-card">
        <p class="card-title">出题前确认几个参数</p>

        <!-- 后端已有权威值：展示即可（例如唯一知识库自动使用） -->
        <p v-for="field in settledFields" :key="`settled-${field.key}`" class="settled-line">
            {{ field.label }}：<b>{{ optionLabel(field, field.value as string | number) }}</b>
            <span v-if="field.note" class="settled-note">{{ field.note }}</span>
        </p>

        <!-- 生成中：不再给表单（再点确认只会撞 400），显示进度并等轮询结果 -->
        <div v-if="generating" class="generating">
            <span class="generating__dots"><i /><i /><i /></span>
            <span class="generating__text">正在出题，通常 30–60 秒；完成后题目会直接出现在这里</span>
        </div>

        <template v-else>
            <el-form label-position="top" class="params-form">
                <el-form-item v-for="field in askedFields" :key="field.key" :label="field.label">
                    <!-- 知识库：下拉（候选来自后端，含文档数 + "不使用知识库"一项） -->
                    <el-select
                        v-if="field.type === 'select'"
                        :model-value="form[field.key] as number | undefined"
                        :placeholder="field.note || '请选择知识库（也可以不使用）'"
                        clearable
                        class="field-control"
                        @update:model-value="setField(field.key, $event)">
                        <el-option
                            v-for="option in field.options ?? []"
                            :key="option.value"
                            :label="optionLabel(field, option.value)"
                            :value="option.value" />
                    </el-select>

                    <!-- 题目数量：数字 -->
                    <el-input-number
                        v-else-if="field.type === 'number'"
                        :model-value="form[field.key] as number | undefined"
                        :min="field.min"
                        :max="field.max"
                        :step="1"
                        class="field-control"
                        @update:model-value="setField(field.key, $event)" />

                    <!-- 发散度 / 难度：单选项（说明文案来自后端） -->
                    <el-radio-group
                        v-else-if="field.type === 'radio'"
                        :model-value="form[field.key] as string | number | undefined"
                        class="radio-group"
                        @update:model-value="setField(field.key, $event)">
                        <el-radio
                            v-for="option in field.options ?? []"
                            :key="option.value"
                            :value="option.value"
                            class="radio-item">
                            <span class="radio-label">{{ option.label }}</span>
                            <span v-if="option.description" class="radio-desc">
                                {{ option.description }}
                            </span>
                        </el-radio>
                    </el-radio-group>

                    <span v-if="field.note" class="field-note">{{ field.note }}</span>
                </el-form-item>
            </el-form>

            <div class="card-actions">
                <el-button type="primary" :loading="submitting" @click="confirm">
                    确认出题
                </el-button>
                <span class="hint">确认后立刻按这些参数出题</span>
            </div>
        </template>

        <p v-if="pendingHint" class="pending-hint">{{ pendingHint }}</p>
    </div>
</template>

<style scoped lang="scss">
.quiz-params-card {
    border: 1px solid #e4e7ed;
    border-radius: 10px;
    padding: 14px 16px;
    margin-top: 10px;
    background: #fbfcff;
}

.card-title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
    margin: 0 0 10px;
}

.settled-line {
    margin: 0 0 10px;
    font-size: 13px;
    color: #606266;

    b {
        color: #303133;
    }
}

.settled-note {
    margin-left: 6px;
    color: #909399;
}

.params-form {
    :deep(.el-form-item) {
        margin-bottom: 12px;
    }
}

.field-control {
    width: min(320px, 100%);
}

.radio-group {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
}

.radio-item {
    height: auto;
    margin-right: 0;
    white-space: normal;

    :deep(.el-radio__label) {
        display: inline-flex;
        flex-direction: column;
        line-height: 1.5;
    }
}

.radio-label {
    font-size: 13px;
}

.radio-desc {
    font-size: 12px;
    color: #909399;
}

.field-note {
    display: block;
    font-size: 12px;
    color: #909399;
    margin-top: 4px;
}

.card-actions {
    display: flex;
    align-items: center;
    gap: 10px;
}

/* 生成中：三个跳动的点 + 说明（不引入图标依赖，纯 CSS） */
.generating {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 0 2px;
}

.generating__dots {
    display: inline-flex;
    gap: 4px;

    i {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: #409eff;
        animation: quiz-dot 1s ease-in-out infinite;

        &:nth-child(2) {
            animation-delay: 0.15s;
        }

        &:nth-child(3) {
            animation-delay: 0.3s;
        }
    }
}

.generating__text {
    font-size: 13px;
    color: #606266;
}

.pending-hint {
    margin: 10px 0 0;
    font-size: 12px;
    color: #596272;
}

@keyframes quiz-dot {
    0%,
    100% {
        opacity: 0.25;
        transform: translateY(0);
    }

    50% {
        opacity: 1;
        transform: translateY(-3px);
    }
}

.hint {
    font-size: 12px;
    color: #909399;
}
</style>
