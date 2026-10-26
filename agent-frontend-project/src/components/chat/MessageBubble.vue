<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChatDotRound, UserFilled } from '@element-plus/icons-vue'
import AgentTrace from './AgentTrace.vue'
import KnowledgeCard from './KnowledgeCard.vue'
import LearningQuizDialog from './LearningQuizDialog.vue'
import LearningQuizParamsCard from './LearningQuizParamsCard.vue'
import MsgFile from './MsgFile.vue'
import { normalizeAgentTrace, traceFromResponsePayload } from '@/utils/agentTrace'
import { renderMarkdownCached } from '@/utils/markdownUtil'
import type {
    AgentRunTrace,
    KnowledgeCardPayload,
    LearningQuizParamsSpec,
    LearningQuizPayload,
    UiMessage,
} from '@/types/chat'

const props = defineProps<{
    message: UiMessage
}>()

const emit = defineEmits<{
    quizSubmitted: []
    /** 用户确认了出题参数（父级要回刷消息：参数卡会被原地换成题目） */
    quizParamsConfirmed: [runId: string]
}>()

const markdownHtml = computed(() => {
    if (props.message.role !== 'assistant') return ''
    // cacheKey 用 message.id，避免旧消息重复解析
    return renderMarkdownCached(props.message.text, props.message.id)
})

// 直接展示后端返回的来源顺序与数量。
const displaySources = computed(() => props.message.sources ?? [])
// 出题消息的判定看**载荷里真的有题**（不是看 request_id 里有没有 "quiz" 字样）：
// 出题评测走 `{type:"learning_quiz", quiz}`，agent loop 委派出题走 worker 落库的同一种消息，
// 两种来源都靠这个判据统一渲染成答题弹窗。
const isLearningQuiz = computed(() => {
    if (props.message.role !== 'assistant') return false
    const payload = props.message.responsePayload as LearningQuizPayload | undefined
    return Boolean(payload?.quiz?.questions?.length)
})

// 参数卡：出题参数还没定（后端只写了卡没出题）。判据是 `payload.params` 有字段，
// 不是 `status === 'awaiting_params'`——status 只是给调试看的，规格才是渲染依据。
// 第 25 轮起还要把 `status` 传给卡：确认后后端把消息状态改成 `generating_quiz`
// （参数卡消息原地变成"正在出题"），刷新页面回来时卡要靠这个状态**接着轮询**，
// 不然用户看到的是一张永远转不完的进度卡。
const quizParams = computed<
    { runId: string; spec: LearningQuizParamsSpec; status: string } | null
>(() => {
    if (props.message.role !== 'assistant') return null
    const payload = props.message.responsePayload as LearningQuizPayload | undefined
    const spec = payload?.params
    const runId = String(payload?.run_id ?? '')
    if (!spec?.fields?.length || !runId) return null
    return { runId, spec, status: String(payload?.status ?? '') }
})

// 知识卡（难点讲解）：判据是**载荷里有卡结构**（与出题卡同一口径——看载荷，不看
// request_id 后缀）。图与结构化卡是同一份内容的两种表达，所以两者同时渲染：
// 图糊了/没出图时，内容仍然读得到（详见 KnowledgeCard.vue 的说明）。
const knowledgeCard = computed<KnowledgeCardPayload | null>(() => {
    if (props.message.role !== 'assistant') return null
    const payload = props.message.responsePayload as KnowledgeCardPayload | undefined
    if (payload?.type !== 'difficulty_card' || !payload.card) return null
    return payload
})

// 有配图时不再提示"本次未生成配图"（degraded 与真实附件以**附件**为准）
const hasCardImage = computed(() =>
    (props.message.segments ?? []).some((segment) => segment.type === 'image'),
)

// Agent 执行轨迹：优先用实时累积的（流式期间），否则从落库的 response_payload 回放。
// 两者形状一致（后端同一个 helper 生成），所以这里只是选一个来源。
//
// 两条来源都要过 `normalizeAgentTrace`：它会**把空轨迹收敛成 null**
// （`{steps:[],tool_calls:[]}` 在 JS 里是真值，直接 v-if 会让每条普通回答
// 下面都挂一个空的「执行轨迹 · 0 步」框）。
const agentTrace = computed<AgentRunTrace | null>(() => {
    if (props.message.role !== 'assistant') return null
    return (
        normalizeAgentTrace(props.message.trace) ??
        traceFromResponsePayload(props.message.responsePayload)
    )
})

// 有轨迹但还没有正文 = 这一轮还在 loop 里跑，用来决定时间线默认展开还是折叠。
const traceRunning = computed(() => Boolean(agentTrace.value) && !props.message.text)

// ---- 流式内联提示（第 12 轮） ----
//
// 之前这段状态是用 `ElLoading.service({ lock: true })` 全屏遮罩表达的，问题是遮罩会把
// 同时在写的流式文字一起盖住：用户按回车只看到"整页加载中"，遮罩撤掉时全文已经一次性
// 出现。现在改为在气泡里表达——正文没到就先显示阶段提示，正文在长就补一个闪烁光标。
const isStreaming = computed(
    () => props.message.role === 'assistant' && Boolean(props.message.streaming),
)

// 正文还没到（loop 里检索/调工具的这几秒）：显示"正在…"提示，避免空气泡看起来像卡死。
// 已经有轨迹时不再显示：那时 AgentTrace 自己就是"执行中 ⏳"的状态位，两个转圈只会互相打架。
const showStreamingHint = computed(
    () => isStreaming.value && !props.message.text && !agentTrace.value,
)

// 正文已经在流：末尾补光标，让"还在继续写"这件事可见。
const showStreamingCaret = computed(() => isStreaming.value && Boolean(props.message.text))

// ---- 思考过程（第 14 轮）----
//
// 思考型模型在给出正文之前会先输出一大段推理。不显示它的话，用户要盯着十几秒
// 毫无变化的界面（实测一句话的回答：全程 2.9s、首个可见文字在 t=2.91s）。
// 显示它之后，这段等待就变成了**看得见的进度**。
//
// 折叠策略：思考期间默认展开，正文一开始就自动收起（正文才是主体，推理退居次要），
// 用户手动点过就听用户的——与 AgentTrace 的 manualOpen 同一套模式。
const hasReasoning = computed(() => Boolean(props.message.reasoning))
const reasoningRunning = computed(() => isStreaming.value && !props.message.text)
const manualReasoningOpen = ref<boolean | null>(null)
const reasoningOpen = computed(() => manualReasoningOpen.value ?? reasoningRunning.value)

const toggleReasoning = () => {
    manualReasoningOpen.value = !reasoningOpen.value
}

// 将 0~1 的相关度分数转换为百分比展示。
const formatScore = (score: number) => `${Math.round(score * 100)}%`

const expandedSourceKeys = ref<Set<string>>(new Set())

// 为每条来源生成本组件内唯一 key，用于渲染和展开状态记录。
const sourceViewKey = (idx: number) => `${props.message.id}-source-${idx}`

// 文本较长时默认折叠，避免来源面板占用过多空间。
const isLongText = (text?: string) => (text?.length ?? 0) > 160

// 判断当前来源正文是否已展开。
const isSourceTextExpanded = (idx: number) => expandedSourceKeys.value.has(sourceViewKey(idx))

// 切换单条来源正文的展开/收起状态。
const toggleSourceText = (idx: number) => {
    const next = new Set(expandedSourceKeys.value)
    const key = sourceViewKey(idx)
    if (next.has(key)) {
        next.delete(key)
    } else {
        next.add(key)
    }
    expandedSourceKeys.value = next
}
</script>

<template>
    <div class="message-row" :class="`message-row--${message.role}`">
        <!-- 头像 -->
        <div class="avatar" :class="`avatar--${message.role}`">
            <el-icon>
                <ChatDotRound v-if="message.role === 'assistant'" />
                <UserFilled v-else />
            </el-icon>
        </div>
        <div class="message-main">
            <!-- 消息气泡 -->
            <div class="bubble" :class="`bubble--${message.role}`">
                <!-- 思考过程：推理先于正文到达，是等待期唯一的可见进度 -->
                <div v-if="hasReasoning" class="thinking">
                    <button type="button" class="thinking__head" @click="toggleReasoning">
                        <span class="thinking__icon">{{ reasoningRunning ? '💭' : '🧠' }}</span>
                        <span class="thinking__title">
                            {{ reasoningRunning ? '思考中…' : '思考过程' }}
                        </span>
                        <span class="thinking__meta">{{ message.reasoning?.length ?? 0 }} 字</span>
                        <span class="thinking__chevron">
                            {{ reasoningOpen ? '收起' : '展开' }}
                        </span>
                    </button>
                    <p v-if="reasoningOpen" class="thinking__body">{{ message.reasoning }}</p>
                </div>
                <!-- AI 回复：使用 Markdown 渲染 -->
                <div
                    v-if="message.role === 'assistant'"
                    class="markdown-body"
                    v-html="markdownHtml" />
                <!-- 用户提问：纯文本显示 -->
                <p v-if="message.role !== 'assistant'" class="text">{{ message.text }}</p>
                <!-- 流式阶段提示：正文未到时的内联占位（取代原来的全屏 loading 遮罩） -->
                <div v-if="showStreamingHint" class="streaming-hint">
                    <span class="streaming-hint__spinner" />
                    <span class="streaming-hint__text">{{
                        message.streamStatus || '正在生成回答…'
                    }}</span>
                </div>
                <!-- 流式光标：正文正在增长时跟在后面闪烁 -->
                <span v-if="showStreamingCaret" class="streaming-caret" />
                <!-- 附件渲染 -->
                <MsgFile
                    v-for="(segment, idx) in message.segments"
                    :key="`${message.id}-${idx}`"
                    :segment="segment" />
                <!-- Agent 执行轨迹：实时与回放共用（见 utils/agentTrace.ts） -->
                <AgentTrace
                    v-if="agentTrace"
                    :trace="agentTrace"
                    :running="traceRunning" />
                <!-- 参考知识片段：仅由 done.sources 决定是否展示 -->
                <details
                    v-if="message.role === 'assistant' && displaySources.length"
                    class="source-collapse">
                    <summary class="source-summary">
                        参考了 {{ displaySources.length }} 个知识片段
                    </summary>
                    <div class="source-list">
                        <div
                            v-for="(source, idx) in displaySources"
                            :key="sourceViewKey(idx)"
                            class="source-item">
                            <div class="source-meta">
                                <span class="source-file">📄 {{ source.file_name }}</span>
                                <span class="source-score"
                                    >相关度 {{ formatScore(source.score) }}</span
                                >
                            </div>
                            <p
                                v-if="source.text"
                                class="source-text"
                                :class="{
                                    'source-text--clamped':
                                        isLongText(source.text) && !isSourceTextExpanded(idx),
                                }">
                                {{ source.text }}
                            </p>
                            <button
                                v-if="isLongText(source.text)"
                                type="button"
                                class="source-toggle"
                                @click="toggleSourceText(idx)">
                                {{ isSourceTextExpanded(idx) ? '收起' : '展开全文' }}
                            </button>
                        </div>
                    </div>
                </details>
                <!-- 难点讲解知识卡：图（附件）之上的结构化内容，二者互补 -->
                <KnowledgeCard
                    v-if="knowledgeCard"
                    :payload="knowledgeCard"
                    :has-image="hasCardImage" />
                <LearningQuizParamsCard
                    v-if="quizParams"
                    :run-id="quizParams.runId"
                    :spec="quizParams.spec"
                    :status="quizParams.status"
                    @confirmed="emit('quizParamsConfirmed', $event)" />
                <LearningQuizDialog
                    v-if="isLearningQuiz"
                    :message="message"
                    :auto-open="message.autoOpenQuiz === true"
                    @submitted="emit('quizSubmitted')" />
            </div>
            <!-- 时间戳 -->
            <p class="time">
                {{
                    new Date(message.createdAt * 1000).toLocaleTimeString('zh-CN', {
                        hour12: false,
                    })
                }}
            </p>
        </div>
    </div>
</template>

<!-- scoped: 仅作用于本组件的基础样式 -->
<style scoped lang="scss">
.message-row {
    display: flex;
    gap: 14px;
    margin-bottom: 20px;
    width: min(760px, 100%);
}

.avatar {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 16px;
    flex-shrink: 0;
}

.avatar--assistant {
    background-color: #4385f1;
}

.avatar--user {
    background-color: #6b778a;
}

.message-main {
    width: calc(100% - 50px);
    max-width: 710px;
}

.bubble {
    border-radius: 14px;
    font-size: 18px;
    line-height: 1.7;
    padding: 14px 18px;
    border: 1px solid transparent;
    color: #2f3642;
}

.text {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
}

.bubble--assistant {
    background: #f2f1ed;
    border-color: #d8dce4;
}

.bubble--user {
    background: #d8dde8;
}

/* ---- 流式内联提示 ----
 * 这几条替代了原来的全屏 ElLoading：状态必须画在气泡里，否则它会盖住流式文字。
 */
.streaming-hint {
    display: flex;
    align-items: center;
    gap: 8px;
    color: #6b7280;
    font-size: 15px;
}

.streaming-hint__spinner {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
    border: 2px solid #d8dce4;
    border-top-color: #4385f1;
    border-radius: 50%;
    animation: streaming-spin 0.8s linear infinite;
}

.streaming-caret {
    display: inline-block;
    width: 8px;
    height: 17px;
    margin-left: 2px;
    vertical-align: -3px;
    border-radius: 1px;
    background: #4385f1;
    animation: streaming-blink 1s steps(2, start) infinite;
}

@keyframes streaming-spin {
    to {
        transform: rotate(360deg);
    }
}

@keyframes streaming-blink {
    0%,
    50% {
        opacity: 1;
    }
    50.01%,
    100% {
        opacity: 0;
    }
}

/* ---- 思考过程 ----
 * 推理可能有几千字，所以正文区限高可滚：不能让它把答案顶到屏幕外面去。
 */
.thinking {
    margin-bottom: 10px;
    border: 1px dashed #cfd6e2;
    border-radius: 8px;
    background: rgba(67, 133, 241, 0.04);
    overflow: hidden;
}

.thinking__head {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    border: 0;
    background: transparent;
    color: #6b7280;
    font-size: 14px;
    text-align: left;
    cursor: pointer;
}

.thinking__title {
    font-weight: 500;
}

.thinking__meta {
    color: #9ea6b4;
    font-size: 13px;
}

.thinking__chevron {
    margin-left: auto;
    color: #9ea6b4;
    font-size: 13px;
}

.thinking__body {
    max-height: 220px;
    overflow-y: auto;
    margin: 0;
    padding: 8px 10px;
    border-top: 1px dashed #dfe4ec;
    color: #7b8290;
    font-size: 14px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
}

.time {
    margin: 7px 0 0;
    color: #9ea6b4;
    font-size: 13px;
}

.source-collapse {
    margin-top: 10px;
    font-size: 14px;
    color: #526173;
}

.source-summary {
    cursor: pointer;
    user-select: none;
    color: #4385f1;
    font-weight: 500;
}

.source-list {
    margin-top: 8px;
    padding: 10px 12px;
    border: 1px solid #d8dce4;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.45);
}

.source-item {
    padding: 8px 0;
    line-height: 1.6;
}

.source-item + .source-item {
    border-top: 1px solid #d8dce4;
}

.source-meta {
    display: flex;
    justify-content: space-between;
    gap: 12px;
}

.source-file {
    word-break: break-word;
}

.source-score {
    flex-shrink: 0;
    color: #7b8290;
}

.source-text {
    margin: 6px 0 0;
    color: #3f4856;
    white-space: pre-wrap;
    word-break: break-word;
}

.source-text--clamped {
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
}

.source-toggle {
    margin-top: 4px;
    padding: 0;
    border: 0;
    background: transparent;
    color: #4385f1;
    cursor: pointer;
}
</style>

<!-- non-scoped: Markdown 渲染内容的全局美化样式 -->
<style>
.markdown-body {
    line-height: 1.7;
    word-break: break-word;
}

.markdown-body p {
    margin: 0 0 10px;
}

.markdown-body p:last-child {
    margin-bottom: 0;
}

.markdown-body strong {
    font-weight: 600;
}

.markdown-body em {
    font-style: italic;
}

.markdown-body a {
    color: #4385f1;
    text-decoration: none;
}

.markdown-body a:hover {
    text-decoration: underline;
}

.markdown-body ul,
.markdown-body ol {
    padding-left: 22px;
    margin: 6px 0;
}

.markdown-body li {
    margin-bottom: 4px;
}

.markdown-body blockquote {
    margin: 10px 0;
    padding: 8px 16px;
    border-left: 4px solid #4385f1;
    background: rgba(67, 133, 241, 0.06);
    border-radius: 0 6px 6px 0;
    color: #555;
}

.markdown-body blockquote p {
    margin: 0;
}

/* 行内代码 */
.markdown-body code {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 0.88em;
    padding: 2px 6px;
    background: rgba(0, 0, 0, 0.06);
    border-radius: 4px;
    color: #d63384;
}

/* 代码块 */
.markdown-body pre.code-block {
    position: relative;
    margin: 12px 0;
    padding: 16px 18px;
    background: #1e1e2e; /* 深色背景 */
    border-radius: 8px;
    overflow-x: auto;
    font-size: 14px;
    line-height: 1.5;
}

/* 代码块右上角语言标识 */
.markdown-body pre.code-block::before {
    content: attr(data-lang);
    position: absolute;
    top: 6px;
    right: 12px;
    font-size: 12px;
    color: rgba(255, 255, 255, 0.4);
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.markdown-body pre.code-block code {
    background: none;
    padding: 0;
    color: #cdd6f4; /* 适配深色背景的文本颜色 */
    font-size: inherit;
}

/* 表格 */
.markdown-body table {
    width: 100%;
    border-collapse: collapse;
    margin: 12px 0;
    overflow-x: auto; /* 支持横向滚动 */
    display: block;
}

.markdown-body table thead {
    background: #e8ecf4;
}

.markdown-body table th,
.markdown-body table td {
    padding: 8px 14px;
    border: 1px solid #d0d5dd;
    text-align: left;
    white-space: nowrap;
}

.markdown-body table tbody tr:nth-child(even) {
    background: #f6f8fc; /* 斑马纹 */
}

.markdown-body table tbody tr:hover {
    background: #e3edfa; /* 悬浮高亮 */
}

/* 标题 */
.markdown-body h1,
.markdown-body h2,
.markdown-body h3,
.markdown-body h4,
.markdown-body h5,
.markdown-body h6 {
    margin: 16px 0 8px;
    line-height: 1.4;
    font-weight: 600;
}

.markdown-body h1 {
    font-size: 1.5em;
}
.markdown-body h2 {
    font-size: 1.3em;
}
.markdown-body h3 {
    font-size: 1.15em;
}
.markdown-body h4 {
    font-size: 1.05em;
}

.markdown-body hr {
    border: none;
    border-top: 1px solid #d0d5dd;
    margin: 16px 0;
}

.markdown-body img {
    max-width: 100%;
    border-radius: 6px;
}

.markdown-body input[type='checkbox'] {
    margin-right: 6px;
}
</style>
