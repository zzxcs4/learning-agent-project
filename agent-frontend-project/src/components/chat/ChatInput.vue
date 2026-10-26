<script setup lang="ts">
import { Paperclip, Promotion, Right, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { UploadFile, UploadInstance, UploadRawFile } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { listKnowledgeBases, quizStatusView, type KnowledgeBaseItem } from '@/api/knowledgeBase'
import { uploadDocument } from '@/api/upload'
import type { ChatSendPayload } from '@/types/chat'
import KnowledgeBaseIcon from './KnowledgeBaseIcon.vue'
import ModePicker, { type ModeCard } from './ModePicker.vue'

const props = defineProps<{
    disabled?: boolean
    /** 候选模式（含默认的「聊天」）；**当前模式由 agentName 承载**：
     *  `undefined` = 默认聊天模式，其余为后端智能体名（见 StudyRoom 的 modes） */
    modes?: ModeCard[]
    /** 当前智能体名；undefined = 默认聊天模式 */
    agentName?: string
    /** 当前会话 id：知识库挂载是**会话级**的（切会话各自记住，见下方挂载表） */
    sessionId?: string | number | null
    /** 输入框下方的示例提问；点了先填进输入框，用户能改完再发 */
    examples?: string[]
    /** 会话里已经有消息时不显示示例（聊天记录本身就是引导） */
    hasMessages?: boolean
}>()

// send：发送文本消息
const emit = defineEmits<{
    send: [payload: ChatSendPayload]
    /** 用户在模式选择器里换了模式（undefined = 回到默认聊天模式）。
     *  事件名不带 `update:` 前缀：**prop 名是 agentName，但它只在父级手里**，
     *  这里不是 v-model（组件内不改 prop），用 `update:agentName` 只会让人误以为能 `v-model:agent-name`。 */
    agentNameChange: [value: string | undefined]
}>()

const text = ref('')
// 只有输入非空且未被禁用时才允许发送
const canSend = computed(() => text.value.trim().length > 0 && !props.disabled)

// ---- 模式选择（第 26 轮 UI 改版）----
//
// 演变过程：
// 1. 原来是**平铺**在输入框底部的几个按钮，且是「多选」语义（`toggleAgent`：
//    再点一次取消，等于回到默认模式）；
// 2. 现在收进 ModePicker，并改成**单选**：模式这个东西本来就是"我现在要用哪种能力"，
//    不存在同时开两个；默认模式（聊天）也是面板里的一个正式选项，而不是"全部关掉"。
//
// 值仍然是用 `agentName` 透传：它是 `ChatSendPayload` 与后端 `agent_name` 的既有名字，
// 改名成 mode 会牵动发送链路与父级，收益只有"字面好看"，不划算。
const selectedAgentName = computed(() => props.agentName)

// 选了模式就收起任何打开的知识库面板？不需要——两者互不干扰。

// ---- 知识库选择：**所有模式常驻** ----
//
// 演变过程（值得留档，因为前后两次结论相反）：
// 1. 最初只属于出题链路；
// 2. 第 2 期起"不选智能体"= 默认进 agent loop，而 loop 里的 `search_material` 会按
//    knowledge_base_id 把检索收窄到本库，于是改成"默认模式 + 出题模式可见，
//    **其余显式智能体不展示，避免改变既有语义**"；
// 3. 现在改为**常驻**：知识卡片（原难点讲解）也要用库（`_knowledge_context` 会按用户这句话
//    检索原文并取考纲）。第 2 步那种"按 agent 白名单决定可见性"的做法，每接一个新智能体
//    就要回来改一次判断，而且漏改的表现是**静默少用资料**（用户看不出原因）——
//    所以改成：控件恒可见，**用不用由后端各自决定**。
//
// 传下去的值有三层含义，与后端口径一致：
// - 默认模式（loop）：`search_material` 的检索范围；
// - 出题：注入考纲 + 决定出题归属；
// - 知识卡片：`_knowledge_context` 的检索范围 + 考纲 + 薄弱点过滤。

const knowledgeBases = ref<KnowledgeBaseItem[]>([])
const loadingKnowledgeBases = ref(false)

// ---- 知识库挂载（第 24 轮：UI 改为"数据库图标 + 点开选择器"，且**会话级**保持）----
//
// 1. 控件精简成一个数据库图标（`KnowledgeBaseIcon`）：没挂载时平时只有图标，鼠标悬停才
//    露出「知识库」字样；挂载后图标旁边直接显示库名。
// 2. 点击图标弹出选择器：第一项恒是「不使用知识库」，下面是用户自己的库。
// 3. 挂载**只在会话内保持**：按会话 id 分开记，切会话各自保留、刷新浏览器丢失（用户确认）。
//
// `MOUNT_NONE`（=0）表示用户**明确**选了"不使用知识库"：它和"从没选过"不同——明确不用库时
// 出题不再追问知识库（后端拿 0 就知道"已定"），从没选过则会被参数卡问一次。
const MOUNT_NONE = 0
const mountedBySession = ref<Record<string, number>>({})
const mountKey = computed(() => String(props.sessionId ?? ''))
/** 本会话当前挂载的知识库 id（undefined = 从没选过，0 = 明确不用）。
 *
 * 写成**函数**而不是 computed：它只被"发送"和"选择器高亮"读一次，
 * 而 computed 在模板里是自动解包的（`mountedKnowledgeBaseId === ...`），
 * 同一个名字在 script 里要写 `.value`、模板里不能写，改起来容易漏。
 * 函数则两边写法完全一致。 */
const mountedKnowledgeBaseId = () => mountedBySession.value[mountKey.value]
const mountedKnowledgeBase = computed(() =>
    knowledgeBases.value.find((kb) => kb.id === mountedKnowledgeBaseId())
)
const pillText = computed(() => {
    if (mountedKnowledgeBaseId() === MOUNT_NONE) return '不使用知识库'
    return mountedKnowledgeBase.value?.name ?? '知识库'
})
const kbPickerVisible = ref(false)

/** 选中一个知识库（或"不使用"）：写入本会话的挂载表并收起选择器 */
const mountKnowledgeBase = (knowledgeBaseId: number) => {
    mountedBySession.value[mountKey.value] = knowledgeBaseId
    kbPickerVisible.value = false
}

// 切会话时收起选择器：否则会停留在上一个会话的面板上（挂载值本身是 computed，自动跟随）
watch(
    () => props.sessionId,
    () => {
        kbPickerVisible.value = false
    }
)

// 出题选库、上传选库、默认模式选库共用同一份列表：只请求一次（幂等）。
// 拉不到库不阻断任何主流程：loop 退化为全库检索、出题退化为只按 request_text、上传退化为只存文件。
const loadKnowledgeBases = async () => {
    if (knowledgeBases.value.length || loadingKnowledgeBases.value) return
    loadingKnowledgeBases.value = true
    try {
        const data = await listKnowledgeBases()
        knowledgeBases.value = data.items
    } catch {
        // 见上方说明：静默失败
    } finally {
        loadingKnowledgeBases.value = false
    }
}

// 知识库列表在**挂载时**就加载：选库控件恒可见，不能再等到"选中某个智能体"才拉，
// 否则用户一进页面看到的是一个空下拉框。
onMounted(() => {
    void loadKnowledgeBases()
})

// 从其它模式切回来时补一次（拉过就不再请求）：
// 控件已恒可见，这里只是保留"首次拉取失败后切模式重试"的机会。
watch(selectedAgentName, () => {
    if (!knowledgeBases.value.length) void loadKnowledgeBases()
})

// ---- 示例提问（第 26 轮）----
//
// 参考图在输入框下方列了几条可点的示例。这里只做"填进输入框"而不是"直接发送"：
// 出题/知识卡片这两个模式都要带主题，用户点完几乎总要把主题词改掉，
// 直接发出去等于替用户提问，还会立刻消耗一次模型调用。
const useExample = (example: string) => {
    if (props.disabled) return
    text.value = example
}
const showExamples = computed(
    () => !props.disabled && !props.hasMessages && Boolean(props.examples?.length)
)

const onSend = () => {
    if (!canSend.value) {
        return
    }
    emit('send', {
        text: text.value.trim(),
        agentName: selectedAgentName.value,
        // 知识库：恒携带（控件常驻）。用不用由后端各路由自己决定——
        // 出题注入考纲，默认模式给 `search_material` 划检索范围，
        // 知识卡片用它检索原文 + 取考纲；不用的路由自然忽略。
        // 0=用户明确"不使用知识库"（出题时不再追问），undefined=没挂载（出题时会问一次）。
        knowledgeBaseId: mountedKnowledgeBaseId(),
        // 发散度/题目数/难度**不在这里传**（第 23 轮）：出题参数改成在对话里问，
        // 由出题 Agent 落一张参数卡让用户选（见 LearningQuizParamsCard.vue）。
        // 传了就等于"已定"，用户就再也看不到那几档选择了。
    })
    text.value = ''
}

// Enter 发送，Shift+Enter 换行
const onKeydown = (event: KeyboardEvent) => {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault()
        onSend()
    }
}

// 允许上传的文件扩展名与 accept 属性字符串
const ACCEPT_EXT = ['.pdf', '.docx', '.txt']
const ACCEPT_ATTR = ACCEPT_EXT.join(',')

// 上传弹窗状态
const dialogVisible = ref(false)
const uploadRef = ref<UploadInstance>()
const fileList = ref<UploadFile[]>([])
// 可选：选中后这次上传会触发后台"向量入库 + 考纲提炼"，进度看知识库页
const uploadKnowledgeBaseId = ref<number | undefined>(undefined)
const submitting = ref(false)

// 上传弹窗是"选库"的第二个入口，与出题选库共用同一份列表
watch(dialogVisible, (visible) => {
    if (visible) void loadKnowledgeBases()
})

// 根据文件名后缀校验是否为允许的文件类型
const isExtAllowed = (fileName: string) => {
    const lower = fileName.toLowerCase()
    return ACCEPT_EXT.some((ext) => lower.endsWith(ext))
}

// 点击附件按钮：重置弹窗状态并打开
const onAttachClick = () => {
    fileList.value = []
    uploadKnowledgeBaseId.value = undefined
    dialogVisible.value = true
}

// 上传前校验：不允许的扩展名直接拦截
const onBeforeUpload = (rawFile: UploadRawFile) => {
    if (!isExtAllowed(rawFile.name)) {
        ElMessage.warning('仅支持 PDF / Word / TXT 文件')
        return false
    }
    return true
}

// 超出数量限制时的提示
const onExceed = () => {
    ElMessage.warning('一次仅支持上传一个文件')
}

// 只保留最后一个选择的文件，避免多文件累积
const onFileChange = (_file: UploadFile, files: UploadFile[]) => {
    fileList.value = files.slice(-1)
}

// 弹窗关闭时重置选择状态（提交中不允许重置）
const onDialogClose = () => {
    if (submitting.value) {
        return
    }
    uploadRef.value?.clearFiles()
    fileList.value = []
    uploadKnowledgeBaseId.value = undefined
}

// 提交按钮可用条件：已选文件 + 未在提交中
const canSubmit = computed(
    () => fileList.value.length > 0 && !submitting.value,
)

// 提交上传：调用 uploadDocument 接口，成功后清空弹窗
const onSubmit = async () => {
    if (!canSubmit.value) {
        return
    }
    const rawFile = fileList.value[0]?.raw
    if (!rawFile) {
        ElMessage.error('文件读取失败，请重新选择')
        return
    }
    if (!isExtAllowed(rawFile.name)) {
        ElMessage.warning('仅支持 PDF / Word / TXT 文件')
        return
    }
    submitting.value = true
    try {
        const res = await uploadDocument(
            rawFile,
            uploadKnowledgeBaseId.value,
        )
        const base = res.is_deduplicated ? '文件已存在，已复用' : '文件上传成功'
        ElMessage.success(
            uploadKnowledgeBaseId.value ? `${base}，正在后台入库并提炼考纲` : base,
        )
        dialogVisible.value = false
        uploadRef.value?.clearFiles()
        fileList.value = []
        uploadKnowledgeBaseId.value = undefined
    } catch (err) {
        const message = err instanceof Error ? err.message : '上传失败，请稍后重试'
        ElMessage.error(message)
    } finally {
        submitting.value = false
    }
}
</script>

<template>
    <section class="chat-input">
        <div class="chat-input__composer">
            <el-input
                v-model="text"
                class="chat-input__field"
                type="textarea"
                resize="none"
                :rows="3"
                :disabled="disabled"
                placeholder="输入你的回答..."
                @keydown="onKeydown" />
            <div class="chat-input__actions">
                <!-- 工具栏顺序（第 26 轮，按用户要求）：模式 → 附件 → 知识库 →（右侧）发送。
                     MCP 指示器**已去掉**：它只在真的接了外部 server 时才出现，
                     而本部署没接，留着只是一个永远不显示的死控件（`api/mcp.ts` 保留备用）。 -->
                <ModePicker
                    v-if="modes?.length"
                    class="toolbar__mode"
                    :modes="modes"
                    :model-value="selectedAgentName"
                    :disabled="disabled"
                    @update:model-value="emit('agentNameChange', $event)" />

                <el-button
                    class="icon-btn"
                    :disabled="disabled"
                    title="上传文档"
                    aria-label="上传文档"
                    @click="onAttachClick">
                    <el-icon><Paperclip /></el-icon>
                </el-button>

                <!-- 知识库挂载（第 24 轮）：数据库图标 + 点开选择器，恒可见（所有模式）。
                     默认模式限定 agent loop 的检索范围、出题注入考纲、知识卡片检索原文+考纲；
                     不用的路由后端自己忽略。 -->
                <el-popover
                    v-model:visible="kbPickerVisible"
                    trigger="click"
                    placement="top-start"
                    :width="330"
                    :disabled="disabled">
                    <template #reference>
                        <button
                            type="button"
                            class="kb-pill"
                            :class="{
                                'kb-pill--idle': mountedKnowledgeBaseId() === undefined,
                                'kb-pill--on': mountedKnowledgeBaseId() !== undefined,
                            }"
                            :disabled="disabled"
                            :aria-label="pillText">
                            <KnowledgeBaseIcon :size="15" />
                            <span class="kb-pill__text">{{ pillText }}</span>
                        </button>
                    </template>

                    <div class="kb-panel">
                        <p class="kb-panel__title">
                            <KnowledgeBaseIcon :size="14" />
                            <span>知识库</span>
                        </p>
                        <p class="kb-panel__desc">
                            把答案锚定到你的资料。选中一个知识库后，它会在整个会话里保持挂载；
                            每当问题需要依据时，智能体就会从中检索。
                        </p>

                        <div class="kb-panel__list">
                            <button
                                type="button"
                                class="kb-option"
                                :class="{ 'kb-option--active': mountedKnowledgeBaseId() === MOUNT_NONE }"
                                @click="mountKnowledgeBase(MOUNT_NONE)">
                                <span class="kb-option__name">不使用知识库</span>
                                <span class="kb-option__hint">只按我的要求回答</span>
                            </button>
                            <button
                                v-for="kb in knowledgeBases"
                                :key="kb.id"
                                type="button"
                                class="kb-option"
                                :class="{ 'kb-option--active': mountedKnowledgeBaseId() === kb.id }"
                                @click="mountKnowledgeBase(kb.id)">
                                <span class="kb-option__name">{{ kb.name }}</span>
                                <span class="kb-option__meta">
                                    <el-tag
                                        size="small"
                                        effect="plain"
                                        :type="quizStatusView(kb.quiz_status).type">
                                        {{ quizStatusView(kb.quiz_status).label }}
                                    </el-tag>
                                    <span class="kb-option__hint">{{ kb.resource_count }} 份资料</span>
                                </span>
                            </button>
                            <p v-if="loadingKnowledgeBases" class="kb-panel__empty">
                                正在加载知识库…
                            </p>
                            <p v-else-if="!knowledgeBases.length" class="kb-panel__empty">
                                你还没有知识库：在「知识库」页新建并上传资料后，就能在这里挂载。
                            </p>
                        </div>

                        <p class="kb-panel__foot">挂载只影响本会话，随时可以在这里改。</p>
                    </div>
                </el-popover>

                <!-- 右侧一组：示例提问 + 发送按钮。
                     顺序是**示例在左、发送在最右**（用户要求"靠近发送按钮"）：
                     这跟"示例放在发送后面"是两回事——后者会把发送按钮挤到中间，
                     最右反而被文字占着，看着就不对。整组用 margin-left:auto 推到右边，
                     空间不够时只有示例被压缩（发送按钮固定尺寸）。 -->
                <div class="toolbar__right">
                    <!-- 示例提问：点了填进输入框，不是直接发送——出题/知识卡片都要带主题，
                         用户点完几乎总要把主题词改掉，直接发出去等于替用户提问。 -->
                    <div v-if="showExamples" class="chat-examples">
                        <button
                            v-for="example in examples"
                            :key="example"
                            type="button"
                            class="chat-examples__item"
                            :title="example"
                            @click="useExample(example)">
                            <span class="chat-examples__text">{{ example }}</span>
                            <el-icon class="chat-examples__arrow"><Right /></el-icon>
                        </button>
                    </div>

                    <el-button class="send-btn" :disabled="!canSend" @click="onSend">
                        <el-icon><Promotion /></el-icon>
                    </el-button>
                </div>
            </div>
        </div>

        <el-dialog
            v-model="dialogVisible"
            title="上传文档"
            width="520px"
            :close-on-click-modal="!submitting"
            :close-on-press-escape="!submitting"
            append-to-body
            @close="onDialogClose">
            <el-upload
                ref="uploadRef"
                v-model:file-list="fileList"
                class="upload-box"
                drag
                :accept="ACCEPT_ATTR"
                :auto-upload="false"
                :limit="1"
                :multiple="false"
                :before-upload="onBeforeUpload"
                :on-exceed="onExceed"
                :on-change="onFileChange">
                <el-icon class="upload-box__icon"><UploadFilled /></el-icon>
                <div class="upload-box__text">将文件拖到此处，或 <em>点击选择文件</em></div>
                <template #tip>
                    <div class="upload-box__tip">仅支持 PDF / Word（doc、docx）/ TXT 文件</div>
                </template>
            </el-upload>

            <div class="upload-form">
                <span class="upload-form__label">知识库</span>
                <el-select
                    v-model="uploadKnowledgeBaseId"
                    class="upload-form__select"
                    clearable
                    placeholder="不选（仅保存文件）"
                    :loading="loadingKnowledgeBases">
                    <el-option
                        v-for="kb in knowledgeBases"
                        :key="kb.id"
                        :label="kb.name"
                        :value="kb.id" />
                </el-select>
            </div>
            <div class="upload-box__tip">
                选择知识库后，该文件会在后台自动完成向量入库与考纲提炼，进度见知识库页
                <template v-if="!loadingKnowledgeBases && !knowledgeBases.length">
                    （还没有知识库，先去知识库页新建）
                </template>
            </div>

            <template #footer>
                <el-button :disabled="submitting" @click="dialogVisible = false">取消</el-button>
                <el-button
                    type="primary"
                    :loading="submitting"
                    :disabled="!canSubmit"
                    @click="onSubmit">
                    提交
                </el-button>
            </template>
        </el-dialog>
    </section>
</template>

<style scoped lang="scss">
.chat-input {
    position: relative;
    background: #f4f5f7;
    border-top: 1px solid #d9dce1;
    padding: 12px 0 10px;
    display: flex;
    flex-direction: column;
    align-items: center;
}

/* 输入框 + 工具栏共用的宽度：与消息列表内层同宽（消息区 100vw-320px 再减左右各 24px），
   之前写的是 calc(100vw - 560px)，比列表窄 240px，明明右边有地方却缩着。
   上限 1040px 保持不变。 */
.chat-input__composer {
    position: relative;
    width: min(992px, calc(100vw - 380px));
    min-width: 560px;
}

.chat-input__field {
    width: 100%;
}

:deep(.el-textarea__inner) {
    height: 130px !important;
    min-height: 92px !important;
    border-radius: 12px;
    background: #f7f8fb;
    border: 1px solid #d3d9e2;
    /* 工具栏浮在框内底部（高 46px）：文字要预留出这块空间，
       否则输入到第三行时会从工具按钮底下穿过去。 */
    padding-bottom: 46px;
    padding-right: 14px;
    font-size: 16px;
    line-height: 1.6;
}

/* 工具栏浮在输入框底部（textarea 本身占满整个框）。
   这里**不用 gap**：示例提问是可伸缩且可能为 0 宽的，flex 的 gap 在 0 宽项两侧照样占位，
   于是窄屏上空出一段、发送按钮看着发飘；改成每个控件自己带右外边距，0 宽项就不占地方。 */
.chat-input__actions {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 46px;
    display: flex;
    align-items: center;
    padding: 0 10px;
    pointer-events: none;
}

.chat-input__actions > * {
    pointer-events: all;
    /* 工具栏里"可点的控件"一律不压缩：模式按钮被挤成两个字、附件/发送被压扁
       都是这一行没写造成的。会伸缩的只有示例提问（它自己 min-width:0）。 */
    flex-shrink: 0;
    margin-right: 10px;
}

/* 最右那个（发送按钮，或它左边的示例列）不要再留右外边距 */
.chat-input__actions > *:last-child {
    margin-right: 0;
}

/* 右侧一组（示例 + 发送）：整组推到最右，让**发送按钮落在最右端**。
   不用 margin-left:auto 的 send 单干：那样示例会被挤到发送按钮右边去。 */
.toolbar__right {
    display: flex;
    align-items: center;
    min-width: 0;
    margin-left: auto;
}

/* 发送按钮自己不带外边距（间距由示例那侧的 padding-left 给） */
.send-btn {
    margin-left: 0;
}

/* ---- 知识库挂载：数据库图标 + 悬停露出字样（第 24 轮） ---- */
.kb-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 32px;
    padding: 0 10px;
    /* 未挂载时也描边（参考图里这三个控件长一个样）：只靠图标分辨不出"能不能点"。
       注意不能给它 `padding: 0 8px` 去凑圆形——空闲态的文字块宽度是 0，
       圆角胶囊会被 flex 的 gap 撑成 6px 的偏心图标。 */
    border: 1px solid #d3d9e2;
    border-radius: 10px;
    background: #ffffff;
    color: #6b7280;
    cursor: pointer;
    transition: border-color 0.16s ease, color 0.16s ease, background 0.16s ease;

    &:hover:not(:disabled) {
        border-color: #a8b3c4;
        color: #303133;
    }

    &:disabled {
        cursor: not-allowed;
        opacity: 0.6;
    }
}

/* 未挂载：只有图标 + 边框，鼠标悬停才把「知识库」字样露出来。
   显式把 gap 收掉：`max-width: 0` 的文字块在 flex 里仍然占一个 gap，
   所以空闲态图标会偏心。 */
.kb-pill--idle {
    gap: 0;
}

/* 已挂载：图标旁常显库名 */
.kb-pill--on {
    border-color: #c6d4ea;
    background: #f2f6fd;
    color: #303133;
}

/* 未挂载：平时只有图标，鼠标悬停才把「知识库」字样露出来 */
.kb-pill__text {
    display: inline-block;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 12px;
    transition: max-width 0.16s ease, opacity 0.16s ease;
}

.kb-pill--idle .kb-pill__text {
    max-width: 0;
    opacity: 0;
}

.kb-pill--idle:hover .kb-pill__text {
    max-width: 90px;
    opacity: 1;
}

/* 悬停展开时把 gap 还回来（否则字样会贴着图标） */
.kb-pill--idle:hover {
    gap: 6px;
}

/* ---- 知识库选择器面板（挂在 el-popover 里） ---- */
.kb-panel__title {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 0 0 4px;
    font-size: 13px;
    font-weight: 600;
    color: #303133;
}

.kb-panel__desc {
    margin: 0 0 10px;
    font-size: 12px;
    line-height: 1.6;
    color: #909399;
}

.kb-panel__list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-height: 260px;
    overflow-y: auto;
}

.kb-panel__empty {
    margin: 6px 0 0;
    font-size: 12px;
    color: #909399;
}

.kb-panel__foot {
    margin: 10px 0 0;
    font-size: 11px;
    color: #c0c4cc;
}

/* 候选行：整行可点（单选） */
.kb-option {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    width: 100%;
    padding: 6px 8px;
    border: 1px solid transparent;
    border-radius: 8px;
    background: transparent;
    text-align: left;
    cursor: pointer;

    &:hover {
        background: #f5f7fa;
    }
}

.kb-option--active {
    border-color: #c6d4ea;
    background: #eef4ff;
}

.kb-option__name {
    font-size: 13px;
    color: #303133;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.kb-option__meta {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
}

.kb-option__hint {
    font-size: 11px;
    color: #909399;
    white-space: nowrap;
}

/* ---- 示例提问：与发送按钮同一行、紧挨它左侧，横向排列 ---- */
.chat-examples {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: nowrap;
    /* 允许被压缩：没有 min-width:0 时 flex 项不肯缩到内容宽度以下，
       窄屏上三个示例会把工具栏（甚至发送按钮）顶出去。 */
    min-width: 0;
    margin-left: auto;
    /* 左侧那 10px 间距自己带（工具栏不用 gap，见 .chat-input__actions 的说明） */
    padding-left: 10px;
    overflow: hidden;
}

.chat-examples__item {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    min-width: 0;
    flex-shrink: 1;
    padding: 4px 8px;
    border: none;
    border-radius: 8px;
    background: transparent;
    color: #6b7280;
    font-size: 13px;
    line-height: 1.5;
    white-space: nowrap;
    cursor: pointer;
    transition: background 0.16s ease, color 0.16s ease;

    &:hover {
        background: #eaecf1;
        color: #303133;
    }
}

/* 文字太长时省略：一行只有这么多地方，宁可截断也不要换行把工具栏撑高 */
.chat-examples__text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.chat-examples__arrow {
    flex-shrink: 0;
    font-size: 12px;
    color: #b6bcc7;
}

.chat-examples__item:hover .chat-examples__arrow {
    color: #6b7280;
}

.icon-btn {
    width: 32px;
    height: 32px;
    padding: 0;
    border-radius: 50%;
    border: 1px solid #d3d9e2;
    background: #ffffff;
    color: #596272;
}

.send-btn {
    width: 34px;
    height: 34px;
    border-radius: 50%;
    border: none;
    background: #000000;
    color: #ffffff;

    &:hover:not(.is-disabled) {
        background: #383838;
    }

    &.is-disabled {
        background: #d1d5dc;
        color: #9ea1a8;
    }
}

.upload-box {
    width: 100%;
}

.upload-box__icon {
    font-size: 48px;
    color: #c0c4cc;
}

.upload-box__text {
    color: #606266;
    font-size: 14px;

    em {
        color: #1f1f1f;
        font-style: normal;
    }
}

.upload-box__tip {
    margin-top: 8px;
    color: #909399;
    font-size: 12px;
}

.upload-form {
    margin-top: 16px;
    display: flex;
    align-items: center;
    gap: 12px;
}

.upload-form__label {
    color: #303133;
    font-size: 14px;
    white-space: nowrap;
}

.upload-form__select {
    flex: 1;
}

@media (max-width: 1100px) {
    .chat-input__composer {
        width: calc(100vw - 130px);
        min-width: 0;
    }
}
</style>
