<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
    createSession,
    deleteSession,
    getSessionMessages,
    getSessions,
    updateSessionTitle,
} from '@/api/chat'
import ChatWindow from '@/components/chat/ChatWindow.vue'
import type { ModeCard } from '@/components/chat/ModePicker.vue'
import AppHeader from '@/components/layout/AppHeader.vue'
import SideBar from '@/components/layout/SideBar.vue'
import { useSessionStreamSender } from '@/composables/useSessionStreamSender'
import { tabs } from '@/composables/useRoomShell'
import type { ChatSendPayload, MessageSegment, SessionItem, SessionRecord, UiMessage } from '@/types/chat'

// 学习室固定会话模式 0。
const SESSION_MODEL: 0 = 0

// ---- 模式清单（第 26 轮 UI 改版）----
//
// 1. 「笔记归档」**不再是一种可选模式**：归档现在只由链路自己触发（评分结束后
//    supervisor 按有无薄弱点委派 `note_archiver`，见后端 a2a/dispatcher），
//    用户点一个按钮就"整理笔记"既没有输入（要整场练习才有料），也和自动归档重复。
//    后端 handler 保留不动——去掉的只是入口。
// 2. 「难点讲解」改名「知识卡片」：它产出的是结构化知识卡（含配图），
//    叫"讲解"会让人以为是一段文字。**只改展示名**，value 仍是 `difficulty_learner`，
//    后端路由与提示词模板一个字都不用动。
// 3. 第一项是默认模式（value 为空 = 不传 agent_name，走常规聊天/agent loop 链路）。
//    空字符串是"默认模式"的哨兵值：`ChatSendPayload.agentName` 是可选字段，
//    真的传 undefined 会被"没选模式"和"没这个字段"混在一起，模式选择器也需要
//    一个能打勾的值（见 ModePicker 的 pick/isSelected）。
const modes: ModeCard[] = [
    { label: '聊天', value: '', hint: '灵活对话，可使用任意工具', icon: 'chat' },
    {
        label: '出题测评',
        value: 'quiz_generate_workflow',
        hint: '按你的知识库出题并自动评分',
        icon: 'quiz',
    },
    {
        label: '知识卡片',
        value: 'difficulty_learner',
        hint: '把难点整理成一张带配图的知识卡',
        icon: 'card',
    },
]

// 三个模式各自点亮对应的示例提问。两条约束：
// 1. 示例文案必须**真的能走到这条链路**上（出题的示例里有"出题/测评"这类关键词，
//    讲解的示例有"讲讲/难点"），否则点了示例却拿到一段普通聊天，用户会以为模式坏了；
// 2. **要短**：示例现在是工具栏里横向排列的一行（在发送按钮左侧），
//    一句 20 字以上的话会被省略号截掉——用户看不到完整问题就不敢点。
//    这里控制在 12 字以内（完整文案仍会在 hover 时以 title 提示）。
const modeExamples: Record<string, string[]> = {
    '': ['帮我看看哪里弱', '什么是向量数据库', '怎么系统学一门新课'],
    quiz_generate_workflow: ['根据知识库出套题', '针对错题再出一组', '来一套随堂小测'],
    difficulty_learner: ['讲讲向量检索原理', '难点：注意力机制', '这道题我没弄懂'],
}

// 当前模式（undefined = 默认聊天模式）；示例提问跟着模式切换
const selectedAgentName = ref<string | undefined>(undefined)
const currentModes = computed(() => modes)
const currentExamples = computed(
    () => modeExamples[selectedAgentName.value ?? ''] ?? modeExamples[''],
)

const route = useRoute()
const router = useRouter()

// 顶部标签与会话列表状态。
const activeTab = ref('study-room')
const sessions = ref<SessionItem[]>([])
const activeSessionId = ref<number | null>(null)
const loadingSessions = ref(false)
// 会话分页状态（左侧）。
const sessionPage = ref(1)
const sessionPageSize = 20
const sessionHasMore = ref(false)

// 消息分页状态（右侧）。
const messagePage = ref(1)
const messagePageSize = 20
const messageHasMore = ref(true)
const messageLoading = ref(false)
const messageLoadingMore = ref(false)

// 以 sessionId 为 key 的消息缓存，支持多会话快速切换回显。
const sessionMessages = ref<Record<string, UiMessage[]>>({})

// 与后端时间字段保持一致：秒级时间戳。
const nowTs = () => Math.floor(Date.now() / 1000)

watch(
    () => route.path,
    (path) => {
        const tab = tabs.find((item) => item.path === path)
        if (tab) activeTab.value = tab.key
    },
    { immediate: true },
)

const handleTabClick = async (tabKey: string) => {
    const selectedTab = tabs.find((tab) => tab.key === tabKey)
    if (!selectedTab) return
    activeTab.value = tabKey
    await router.push(selectedTab.path)
}

// 当前激活会话对应的消息列表（空会话返回空数组）。
const activeMessages = computed(() => {
    const id = activeSessionId.value
    if (!id) return []
    return sessionMessages.value[String(id)] ?? []
})

// 后端一条记录包含“问+答”，前端拆成两条消息气泡。
//
// **卡片型消息只保留助手气泡**（`{run_id}:quiz` 出题卡 / `{run_id}:difficulty` 知识卡）：
// 它们本质是"结果卡"，真正的用户原话在它前面那条普通聊天里已经显示过了。第 25 轮起
// request_text 被强制写成用户原话，再拆一个用户气泡就会同一句话显示两次（用户 2026-10-19 报）。
const isCardOnlyRecord = (requestId?: string) => {
    const id = requestId ?? ''
    return id.endsWith(':quiz') || id.endsWith(':difficulty')
}

const mapRecordsToUiMessages = (records: SessionRecord[]): UiMessage[] => {
    const sorted = [...records].sort((a, b) => a.created_at - b.created_at)
    return sorted.flatMap((item) => {
        const assistantMessage: UiMessage = {
            id: `${item.id}-resp`,
            role: 'assistant',
            text: item.response_text || '',
            createdAt: item.created_at,
            segments: item.response_segments || [],
            requestId: item.request_id,
            responsePayload: item.response_payload,
            sources: item.sources || undefined,
            status: item.status,
        }
        if (isCardOnlyRecord(item.request_id)) {
            // 只保留卡片气泡，不要重复的用户消息
            return [assistantMessage]
        }
        const userMessage: UiMessage = {
            id: `${item.id}-req`,
            role: 'user',
            text: item.request_text || '',
            createdAt: item.created_at,
            segments: item.request_segments || [],
        }
        return [userMessage, assistantMessage]
    })
}

// 拉取消息：reset=true 用服务端第一页**整页替换**当前缓存（不先清空，所以不会闪），
// 否则追加更早的一页（上拉分页）。
//
// 为什么不是"不清空就直接追加第一页"（第 22 轮修掉的重复气泡）：回刷时列表里已经有流式
// 期间渲染过的那批消息，再追加一次服务端第一页 = 把同一条消息**又插一遍**，每条两份气泡。
//
// freshQuizRunIds：本轮**刚出题**的 run_id。只给它们对应的题目消息挂自动弹窗标记；
// 从历史加载 / 切会话回来的消息一律不挂，否则一个会话里有几套题就会同时弹出几个弹窗。
const loadMessages = async (reset = false, freshQuizRunIds: string[] = []) => {
    if (!activeSessionId.value) return
    if (messageLoading.value || messageLoadingMore.value) return

    if (reset) {
        messageLoading.value = true
        messagePage.value = 1
        messageHasMore.value = true
    } else {
        if (!messageHasMore.value) return
        messageLoadingMore.value = true
    }

    try {
        const data = await getSessionMessages(activeSessionId.value, {
            page: messagePage.value,
            page_size: messagePageSize,
            session_model: SESSION_MODEL,
        })
        const key = String(activeSessionId.value)
        const next = mapRecordsToUiMessages(data.messages)
        markFreshQuizzes(next, freshQuizRunIds)
        sessionMessages.value[key] = reset
            ? next
            : [...(sessionMessages.value[key] ?? []), ...next]
        messageHasMore.value = data.has_more
        messagePage.value += 1
    } finally {
        messageLoading.value = false
        messageLoadingMore.value = false
    }
}

/** 给"本轮新出的题"打自动弹窗标记：出题消息的 request_id 是 `{run_id}:quiz`。 */
const markFreshQuizzes = (messages: UiMessage[], runIds: string[]) => {
    if (!runIds.length) return
    const prefixes = runIds.map((runId) => `${runId}:quiz`)
    messages.forEach((message) => {
        if (prefixes.includes(message.requestId ?? '')) {
            message.autoOpenQuiz = true
        }
    })
}

/** 摘掉缓存里所有"本轮刚出的题"标记。
 *
 * 标记只对**产生它的那一轮**有效：切走再切回来时，非活动会话的缓存还留着旧标记，
 * 气泡重新挂载就会又弹一次答题窗（用户 2026-10-19 报的"切回会话时同时弹两个题"）。
 * 活动会话的缓存会被 `loadMessages(true)` 整页替换，所以这里只需清缓存里的旧标记。
 */
const clearFreshQuizFlags = () => {
    Object.values(sessionMessages.value).forEach((list) => {
        list.forEach((message) => {
            if (message.autoOpenQuiz) message.autoOpenQuiz = false
        })
    })
}

// 切会话 / 新建会话后"刚出的题"就不再新鲜了：pre-flush 的 watcher 在重渲染之前清标记，
// 因此缓存里的旧题目消息不会再自动弹窗（首屏自动选中会话时缓存还是空的，等于空跑）。
watch(activeSessionId, clearFreshQuizFlags)

// 拉取会话列表：reset 场景用于首屏初始化或整体刷新。
const loadSessions = async (reset = false) => {
    if (loadingSessions.value) return
    loadingSessions.value = true
    try {
        if (reset) {
            sessions.value = []
            sessionPage.value = 1
        }
        const data = await getSessions({
            page: sessionPage.value,
            page_size: sessionPageSize,
            session_model: SESSION_MODEL,
        })
        sessions.value = reset ? data.sessions : [...sessions.value, ...data.sessions]
        sessionHasMore.value = data.has_more
        sessionPage.value += 1
        const first = sessions.value[0]
        if (!activeSessionId.value && first) {
            activeSessionId.value = first.id
            await loadMessages(true)
        }
    } finally {
        loadingSessions.value = false
    }
}

// 新建会话：创建后自动切换并进入该会话消息页。
const createNewSession = async () => {
    try {
        const promptResult = (await ElMessageBox.prompt('请输入会话主题标题', '新建会话', {
            inputValue: '学习主题',
            inputPattern: /.+/,
            inputErrorMessage: '标题不能为空',
        })) as { value: string }
        const result = await createSession({
            title: promptResult.value.trim(),
            session_model: SESSION_MODEL,
        })
        sessions.value.unshift(result.session)
        stopActiveStream()
        activeSessionId.value = result.session.id
        await loadMessages(true)
    } catch {
        // 用户取消时不提示
    }
}

// 切换会话：先停流再切换，防止旧流写入新会话消息。
const handleSessionClick = async (id: number | string) => {
    const nextId = Number(id)
    if (Number.isNaN(nextId) || nextId === activeSessionId.value) return
    stopActiveStream()
    activeSessionId.value = nextId
    await loadMessages(true)
}

// 编辑标题并回写本地会话列表，保持 UI 与后端一致。
const editSessionTitle = async (id: number | string) => {
    const sessionId = Number(id)
    const target = sessions.value.find((item) => item.id === sessionId)
    if (!target) return
    try {
        const promptResult = (await ElMessageBox.prompt('请输入新的会话标题', '编辑标题', {
            inputValue: target.title,
            inputPattern: /.+/,
            inputErrorMessage: '标题不能为空',
        })) as { value: string }
        const result = await updateSessionTitle(
            sessionId,
            { title: promptResult.value.trim() },
            SESSION_MODEL,
        )
        const index = sessions.value.findIndex((item) => item.id === sessionId)
        if (index >= 0) sessions.value[index] = result.session
        ElMessage.success('会话标题已更新')
    } catch {
        // 用户取消时不提示
    }
}

// 删除会话并清理缓存；若删的是当前会话则自动切换到下一会话。
const removeSession = async (id: number | string) => {
    const sessionId = Number(id)
    const target = sessions.value.find((item) => item.id === sessionId)
    if (!target) return
    try {
        await ElMessageBox.confirm(`确认删除会话「${target.title}」吗？`, '删除会话', {
            type: 'warning',
        })
        await deleteSession(sessionId, SESSION_MODEL)
        sessions.value = sessions.value.filter((item) => item.id !== sessionId)
        delete sessionMessages.value[String(sessionId)]
        if (activeSessionId.value === sessionId) {
            stopActiveStream()
            const next = sessions.value[0]
            activeSessionId.value = next ? next.id : null
            if (next) {
                await loadMessages(true)
            } else {
                messageHasMore.value = false
            }
        }
        ElMessage.success('会话已删除')
    } catch {
        // 用户取消时不提示
    }
}

// 通用流式发送：统一处理占位、delta 拼接、结束同步、错误提示。
const { sending, stopActiveStream, send } = useSessionStreamSender({
    sessionModel: SESSION_MODEL,
    getActiveSessionId: () => activeSessionId.value,
    // 构建一对“用户+助手”临时消息，先渲染再接收流式增量。
    createPending: (text, sessionId) => {
        const key = String(sessionId)
        const list = [...(sessionMessages.value[key] ?? [])]
        const baseId = `${sessionId}-${Date.now()}`
        const userMessage: UiMessage = {
            id: `${baseId}-user`,
            role: 'user',
            text,
            createdAt: nowTs(),
            segments: [],
        }
        const assistantMessage = reactive<UiMessage>({
            id: `${baseId}-assistant`,
            role: 'assistant',
            text: '',
            createdAt: nowTs(),
            segments: [] as MessageSegment[],
        })
        list.push(userMessage, assistantMessage)
        sessionMessages.value[key] = list
        return {
            // 每个增量片段实时追加到助手消息。
            appendDelta: (delta) => {
                assistantMessage.text += delta
            },
            // 思考型模型的推理过程：单独累积，**不混进 text**（它不是答案）。
            // 只活在流式期间：后端不落库，刷新后自然消失（见 UiMessage.reasoning 的说明）。
            appendReasoning: (delta) => {
                assistantMessage.reasoning = (assistantMessage.reasoning ?? '') + delta
            },
            // agent loop 的步骤时间线：直接替换引用，触发 AgentTrace 重渲染。
            onTrace: (trace) => {
                assistantMessage.trace = trace
            },
            // 流式阶段状态：气泡内联显示"正在推理/正在调用 X"（取代原来的全屏 loading 遮罩）。
            onStatus: (status) => {
                assistantMessage.streaming = status.streaming
                assistantMessage.streamStatus = status.text
            },
            // 流完成后使用服务端完整文本兜底，并补充参考来源。
            finish: (fullText, sources) => {
                assistantMessage.text = fullText || assistantMessage.text
                assistantMessage.sources =
                    Array.isArray(sources) && sources.length ? sources : undefined
            },
        }
    },
    syncAfterDone: async (sessionId, result) => {
        if (activeSessionId.value === sessionId) {
            // 回刷是为了拿到落库后的 id / request_id / response_payload（出题弹窗与轨迹回放要用）。
            // JSON 路径必然是学习工作流（出题），所以带 run_id 认领本次新出的题；
            // 整页替换而不是清空再追加，避免那条消息出现两份气泡。
            await loadMessages(true, result?.run_id ? [result.run_id] : [])
        }
    },
    // SSE（agent loop）路径：普通问答按设计零回刷；**只有真的委派了出题**才补一次，
    // 否则 worker 落库的那条题目消息在界面上不存在，用户看不到答题弹窗。
    onQuizGenerated: async (sessionId, runIds) => {
        if (activeSessionId.value === sessionId) {
            await loadMessages(true, runIds)
        }
    },
    // 同理：委派了难点讲解时，那条知识卡消息（图挂在 response_segments 里）也要回刷出来。
    // 不带 run_id 当"刚出的题"认领——知识卡不是弹窗，回刷即渲染，没有"新鲜"这回事。
    onDifficultyCardGenerated: async (sessionId) => {
        if (activeSessionId.value === sessionId) {
            await loadMessages(true)
        }
    },
    onError: (message) => ElMessage.error(message),
})

// 发送入口：调用 composable 内的 send。
const handleSend = async (payload: ChatSendPayload) => {
    await send(payload)
}

/** 用户确认了出题参数（第 23 轮）：出题 run 已把那条消息从参数卡**原地**换成题目，
 * 所以带上 run_id 回刷一次——题目消息会挂上 autoOpenQuiz，答题弹窗自动弹出来。 */
const handleQuizParamsConfirmed = async (runId: string) => {
    await loadMessages(true, runId ? [runId] : [])
}

// 进入页面后预加载会话列表；循环拉取到上限以提升切换体验。
onMounted(async () => {
    await loadSessions(true)
    while (sessionHasMore.value && sessions.value.length < 500) {
        await loadSessions(false)
    }
})

// 退出页面时中断活动流，避免状态残留。
onBeforeUnmount(() => {
    stopActiveStream()
})
</script>

<template>
    <div class="study-room">
        <AppHeader :tabs="tabs" :active-tab="activeTab" @tab-click="handleTabClick" />
        <div class="study-room__content">
            <SideBar
                :sessions="sessions"
                :active-session-id="activeSessionId ?? ''"
                @create-session="createNewSession"
                @session-click="handleSessionClick"
                @edit-session="editSessionTitle"
                @remove-session="removeSession" />
            <ChatWindow
                :messages="activeMessages"
                :has-more="messageHasMore"
                :loading-more="messageLoadingMore || messageLoading"
                :disabled="sending || !activeSessionId"
                :modes="currentModes"
                :agent-name="selectedAgentName"
                :examples="currentExamples"
                :session-id="activeSessionId"
                @load-more="loadMessages(false)"
                @quiz-submitted="loadMessages(true)"
                @quiz-params-confirmed="handleQuizParamsConfirmed"
                @agent-name-change="selectedAgentName = $event"
                @send="handleSend" />
        </div>
    </div>
</template>

<style scoped lang="scss">
.study-room {
    width: 100%;
    height: 100vh;
    display: flex;
    flex-direction: column;
}

.study-room__content {
    flex: 1;
    min-height: 0;
    display: flex;
}
</style>
