// 临时探针：编译全部 SFC（模板 + script setup + scss），验证第 2 期前端改动没有模板级错误。
// 跑法：node sfc_check.cjs   （必须放在 agent-frontend-project 下才能解析 @vue/compiler-sfc）
const fs = require('fs')
const path = require('path')
const sfc = require('@vue/compiler-sfc')

const root = path.join(process.cwd(), 'src')
function walk(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) walk(p, out)
    else if (e.name.endsWith('.vue')) out.push(p)
  }
  return out
}

const files = walk(root)
let failed = 0
for (const file of files) {
  const source = fs.readFileSync(file, 'utf8')
  const rel = path.relative(process.cwd(), file)
  const { descriptor, errors } = sfc.parse(source, { filename: file })
  if (errors.length) {
    failed++
    console.log(`PARSE FAIL ${rel}`)
    for (const err of errors) console.log('   ', err.message)
    continue
  }
  try {
    if (descriptor.script || descriptor.scriptSetup) {
      sfc.compileScript(descriptor, { id: rel })
    }
    if (descriptor.template) {
      const res = sfc.compileTemplate({
        source: descriptor.template.content,
        filename: rel,
        id: rel,
        compilerOptions: { expressionPlugins: ['typescript'] },
      })
      if (res.errors && res.errors.length) {
        failed++
        console.log(`TEMPLATE FAIL ${rel}`)
        for (const err of res.errors) console.log('   ', err.message || err)
        continue
      }
    }
    for (const style of descriptor.styles || []) {
      if (style.lang === 'scss') {
        const r = sfc.compileStyle({
          source: style.content,
          filename: rel,
          id: rel,
          scoped: style.scoped,
          preprocessLang: 'scss',
        })
        if (r.errors && r.errors.length) {
          failed++
          console.log(`STYLE FAIL ${rel}`)
          for (const err of r.errors) console.log('   ', err.message || err)
        }
      }
    }
  } catch (err) {
    failed++
    console.log(`COMPILE FAIL ${rel}: ${err.message}`)
  }
}

// ---- 组件接线检查：SFC 能编译 ≠ 真的被用上 ----
// 这些断言的是"接线"，vite 不会因为漏了它而报错，但功能会静默失效。
const wiring = [
  ['src/components/chat/MessageBubble.vue', 'AgentTrace', '消息气泡没有渲染执行轨迹'],
  ['src/views/StudyRoom.vue', 'onTrace', 'StudyRoom 没有把实时轨迹写回消息'],
  ['src/composables/useSessionStreamSender.ts', 'createTraceAccumulator', 'sender 没有累积轨迹'],
  ['src/composables/useSessionStreamSender.ts', "'step'", 'sender 没有处理 step 事件'],
  // 未归档资料（删库留下的孤儿资料）：接口定义了但没人调用 = 入口白做
  ['src/views/KnowledgeBaseRoom.vue', 'listUnboundResources', '知识库页没有拉未归档资料'],
  ['src/views/KnowledgeBaseRoom.vue', 'reassignUnboundResource', '未归档资料没有归还入口'],
  ['src/views/KnowledgeBaseRoom.vue', 'deleteUnboundResource', '未归档资料没有删除入口'],
  ['src/api/knowledgeBase.ts', 'unbound-resources', '接口层没有指向后端未归档路由'],
  // 知识库只收文档（2026-10-20）：选文件这一步必须把图片/音频挡在外面（后端同样口径会 400）
  ['src/views/KnowledgeBaseRoom.vue', 'accept=".pdf,.doc,.docx,.txt"', '知识库上传框没限制文件类型（又能选到 png/音频）'],
  // 库内删除文档（2026-10-20）：入口与接口调用缺一个，这个功能就"看起来做了其实没做"
  ['src/components/knowledge/KnowledgeDocumentReader.vue', 'deleteKnowledgeBaseResource', '文档阅读器没有调用删除接口'],
  // 轨迹明细（第 3 期补齐）：参数、成败、结果预览、每步文本预览
  ['src/components/chat/AgentTrace.vue', 'formatToolArgs', '轨迹没有显示工具参数（只看得到"检索知识库"）'],
  ['src/components/chat/AgentTrace.vue', 'result_preview', '轨迹没有显示工具结果/失败原因'],
  ['src/components/chat/AgentTrace.vue', 'step.text_preview', '轨迹没有显示每一步模型的文本预览'],
  ['src/components/chat/AgentTrace.vue', 'isMcpCall', '轨迹没有把 MCP 工具单独标出来'],
  ['src/composables/useSessionStreamSender.ts', 'result_preview', 'sender 没有把 tool_result 的结果合并进轨迹'],
  ['src/composables/useSessionStreamSender.ts', "current.kind = 'tool'", 'sender 没有把"要调工具"的步骤升级（实时显示会一直是"推理中"）'],
  // MCP 状态接口（第 3 期）：接口层仍在（备用），但输入区按第 26 轮要求**去掉了指示器**，
  // 所以反向断言改成"它没有被接线"，避免有人把死控件又挂回去。
  ['src/api/mcp.ts', '/agent/mcp/servers', '接口层没有指向后端 MCP 状态路由'],
  // 流式显示（第 12 轮）：全屏 loading 会把流式文字一起盖住，状态必须进气泡
  ['src/composables/useSessionStreamSender.ts', 'onStatus', 'sender 没有把流式阶段状态推给气泡'],
  ['src/composables/useSessionStreamSender.ts', 'toolLabel', 'sender 没有把工具名翻成可读文案'],
  ['src/composables/useSessionStreamSender.ts', 'hasContent', 'sender 会推空轨迹（普通回答下面挂一个「执行轨迹 · 0 步」）'],
  ['src/components/chat/MessageBubble.vue', 'streamStatus', '气泡没有内联显示流式阶段'],
  ['src/components/chat/MessageBubble.vue', 'showStreamingCaret', '气泡没有流式光标（看不出还在继续写）'],
  ['src/components/chat/MessageBubble.vue', 'normalizeAgentTrace', '气泡没有把空轨迹收敛掉（会渲染空轨迹框）'],
  ['src/components/chat/MessageList.vue', 'stickToBottom', '消息列表不会跟着流式增量滚动（越写越靠下，看不见）'],
  ['src/views/StudyRoom.vue', 'onStatus', 'StudyRoom 没有把流式状态写回消息'],
  ['src/views/StudyRoom.vue', 'sessionMessages.value[key] = reset', 'StudyRoom 回刷没有整页替换（不清空就追加会把同一条消息插成两份气泡）'],
  // 出题弹窗（第 22 轮）：默认模式（agent loop）委派出题后，worker 落库的题目消息要能被认领
  ['src/composables/useSessionStreamSender.ts', 'onQuizGenerated', 'agent loop 出了题却没人回刷消息列表（答题弹窗根本不出现）'],
  ['src/views/StudyRoom.vue', 'markFreshQuizzes', 'StudyRoom 没有给"本轮新出的题"打自动弹窗标记'],
  ['src/views/StudyRoom.vue', 'clearFreshQuizFlags', '切会话后没清"刚出的题"标记（切回来会再弹一次答题窗）'],
  ['src/components/chat/LearningQuizDialog.vue', 'props.autoOpen', '答题弹窗没有"只对本轮新出的题自动弹出"的开关（切会话回来会一起弹）'],
  ['src/components/chat/MessageBubble.vue', 'payload?.quiz?.questions?.length', '出题识别没看载荷里到底有没有题（默认模式出的题认不出来）'],
  // 全屏答题（第 22 轮）：长题干 + 简答题在 720px 里做卷很难受
  ['src/components/chat/LearningQuizDialog.vue', ':fullscreen="answerFullscreen"', '答题弹窗没有全屏开关'],
  ['src/components/chat/LearningQuizDialog.vue', ':fullscreen="resultFullscreen"', '评分结果弹窗没有全屏开关'],
  ['src/components/chat/QuizAnswerForm.vue', "'update:modelValue'", '答题表单没有做成受控组件（两个入口没法共用）'],
  // 练习补答（第 22 轮）：未作答的题要能补答，而不是只给一份参考评分
  ['src/components/profile/PracticeHistory.vue', 'submitPracticeAnswers', '练习记录没有补答入口'],
  ['src/components/profile/PracticeHistory.vue', 'pendingQuestions', '练习记录没有把"未作答的题"分出来'],
  ['src/components/profile/PracticeHistory.vue', ':pending-ids="pendingIds"', '回看没把未作答的题告诉结果组件（未作答也会显示参考评分与点评）'],
  ['src/api/practice.ts', '/answers', '接口层没有指向后端补答路由'],
  // 思考过程（第 14 轮）：思考型模型 85% 的等待时间只有推理内容可看
  ['src/api/streamChat.ts', "'reasoning'", '事件类型里没有 reasoning（解析器会把它当未知帧丢掉）'],
  ['src/composables/useSessionStreamSender.ts', 'appendReasoning', 'sender 没有把 reasoning 事件交给气泡'],
  ['src/views/StudyRoom.vue', 'appendReasoning', 'StudyRoom 没有累积思考内容'],
  ['src/views/StudyRoom.vue', 'assistantMessage.reasoning', '思考内容没有落到消息上'],
  ['src/components/chat/MessageBubble.vue', 'message.reasoning', '气泡没有渲染思考内容'],
  ['src/components/chat/MessageBubble.vue', 'reasoningRunning', '思考块没有"思考中/思考过程"状态（无法自动收起）'],
  ['src/types/chat.ts', 'reasoning?: string', 'UiMessage 上没有 reasoning 字段'],
  // 出题参数卡（第 23 轮）：参数从"发送前表单"改成"对话里问"，这条链最怕接线断了
  ['src/components/chat/LearningQuizParamsCard.vue', 'submitLearningWorkflowParams', '参数卡没有提交入口（用户只能看着参数卡没法确认）'],
  ['src/components/chat/LearningQuizParamsCard.vue', 'spec.fields', '参数卡没有按后端规格泛型渲染（口径会散到前端）'],
  ['src/components/chat/LearningQuizParamsCard.vue', 'resolved === true', '参数卡没有区分"后端已有权威值/还需要问"（会重复问唯一知识库）'],
  ['src/components/chat/LearningQuizParamsCard.vue', 'resource_count', '知识库候选没有显示资料份数（选库只能靠猜）'],
  ['src/components/chat/MessageBubble.vue', 'quizParams', '气泡没有渲染参数卡（参数没齐时用户看不到任何可点的东西）'],
  ['src/components/chat/MessageBubble.vue', 'payload?.params', '参数卡识别没看载荷里有没有规格'],
  ['src/components/chat/MessageList.vue', 'quiz-params-confirmed', '消息列表没有把"参数已确认"传上去（消息不会回刷）'],
  ['src/components/chat/ChatWindow.vue', 'quizParamsConfirmed', '聊天窗口没有转发出题参数确认事件'],
  ['src/views/StudyRoom.vue', 'handleQuizParamsConfirmed', 'StudyRoom 没有回刷消息（参数卡不会变成题目、答题弹窗不出现）'],
  ['src/views/StudyRoom.vue', 'loadMessages(true, runId ? [runId] : [])', '参数确认后回刷没带 run_id（题目消息认领不到 = 不自动弹答题窗）'],
  ['src/api/learningWorkflow.ts', '/params', '接口层没有指向后端出题参数确认路由'],
  // 参数在对话里问之后，输入框里不该再有发散度控件（留着就等于"已定"，用户看不到选择）
  ['src/components/chat/ChatInput.vue', '发散度/题目数/难度**不在这里传**', '输入框还在传发散度/难度（参数卡会被短路）'],
  // 知识库挂载（第 24 轮）：控件精简成数据库图标 + 点开选择器，且挂载是**会话级**的
  ['src/components/chat/KnowledgeBaseIcon.vue', 'ellipse', '知识库图标不是数据库圆柱（参考图形态对不上）'],
  ['src/components/chat/ChatInput.vue', 'kb-pill', '输入区没有图标按钮（悬停显示字样、点击开选择器都挂在它上面）'],
  ['src/components/chat/ChatInput.vue', 'kbPickerVisible', '点击图标没有弹出知识库选择器'],
  ['src/components/chat/ChatInput.vue', 'mountedBySession', '知识库挂载没有按会话记（切会话会串库）'],
  ['src/components/chat/ChatInput.vue', 'MOUNT_NONE', '选择器里没有「不使用知识库」那一项（用户没法明确不用库）'],
  // 第 26 轮起 `mountedKnowledgeBaseId` 是函数（见 ChatInput 里的说明），断言跟着改成调用形式。
  ['src/components/chat/ChatInput.vue', 'knowledgeBaseId: mountedKnowledgeBaseId()', '发送时没带本会话挂载的知识库（出题还会再问一次库）'],
  ['src/components/chat/ChatInput.vue', '不使用知识库', '选择器里没有"不使用知识库"文案'],
  ['src/components/chat/ChatInput.vue', '把答案锚定到你的资料', '选择器里没有说明"知识库是干什么的"'],
  ['src/components/chat/ChatWindow.vue', ':session-id="sessionId"', '聊天窗口没有把会话 id 透给输入区（挂载表拿不到会话键）'],
  ['src/views/StudyRoom.vue', ':session-id="activeSessionId"', 'StudyRoom 没有把当前会话 id 传给聊天窗口'],
  ['src/components/chat/LearningQuizParamsCard.vue', 'if (count === undefined) return option.label', '参数卡的「不使用知识库」被当成知识库（会显示"暂无资料"）'],
  // 出题异步化（第 25 轮）：`/params` 立即返回 generating_quiz，前端轮询运行状态；
  // 刷新页面回来还要能接着轮询（靠消息 payload 的 status 恢复）。
  ['src/api/learningWorkflow.ts', 'fetchLearningWorkflow', '接口层没有运行状态查询（前端没法轮询后台出题）'],
  ['src/components/chat/LearningQuizParamsCard.vue', 'fetchLearningWorkflow', '参数卡没有轮询运行状态（出题完后题目不会自动出现）'],
  ['src/components/chat/LearningQuizParamsCard.vue', 'POLL_INTERVAL_MS', '参数卡没有轮询间隔（要么不轮询、要么打爆后端）'],
  ['src/components/chat/LearningQuizParamsCard.vue', "status === 'generating_quiz'", '参数卡不认后端的"正在出题"状态（刷新回来会卡在一张转不完的卡）'],
  ['src/components/chat/LearningQuizParamsCard.vue', 'onBeforeUnmount', '参数卡卸载时没停轮询（切会话后还在定时打接口）'],
  ['src/components/chat/LearningQuizParamsCard.vue', '正在出题', '生成中没有给用户进度文案（点完确认像卡死）'],
  ['src/components/chat/MessageBubble.vue', ':status="quizParams.status"', '消息没把 payload 状态传给参数卡（刷新后恢复不了轮询）'],
  // 评分异步化（第 25 轮）：提交答案立即返回 `grading`，答题窗轮询运行状态；
  // 刷新页面回来要能接着轮询（靠消息 payload 的 status），完成后自动弹结果。
  ['src/components/chat/LearningQuizDialog.vue', 'fetchLearningWorkflow', '答题窗没有轮询运行状态（评分完了结果不会自动出现）'],
  ['src/components/chat/LearningQuizDialog.vue', "response.status === 'grading'", '答题窗不认后端的"正在评分"状态（同步等会被 30s 超时掐断）'],
  ['src/components/chat/LearningQuizDialog.vue', "payload.value?.status === 'grading'", '刷新回来不会接着轮询（卡在一张已经交过的卷上）'],
  ['src/components/chat/LearningQuizDialog.vue', 'onBeforeUnmount', '答题窗卸载时没停轮询（切会话后还在定时打接口）'],
  ['src/components/chat/LearningQuizDialog.vue', '正在评分', '评分生成中没有给用户进度文案（点完提交像卡死）'],
  ['src/components/chat/LearningQuizDialog.vue', 'restoreSubmittedAnswers', '刷新回来结果里"我的答案"是空的（答案在运行状态里没取回来）'],
  ['src/api/learningWorkflow.ts', 'next_agent', '结果类型缺 next_agent（A2A 接力字段对不上后端）'],
  // 模式选择器（第 26 轮 UI 改版）：模式从"平铺按钮"收成"一个选择器"，并下线「笔记归档」入口。
  // 这一改动的风险全部在**接线**上：选中的模式没传上去 = 界面上点了但后端还是聊天模式。
  ['src/components/chat/ModePicker.vue', 'mode-panel__item', '模式选择器没有渲染候选面板'],
  ['src/components/chat/ModePicker.vue', 'mode-panel__hint', '模式面板没有显示每个模式做什么（用户只能靠名字猜）'],
  ['src/components/chat/ModePicker.vue', "'update:modelValue'", '模式选择器没有把选中的模式回给父级'],
  ['src/components/chat/ModePicker.vue', 'mode-trigger__caret', '触发按钮没有下拉箭头（看不出这里能点开）'],
  ['src/components/chat/ChatInput.vue', 'ModePicker', '输入区没有用模式选择器（模式还是平铺的）'],
  ['src/components/chat/ChatInput.vue', 'agentNameChange', '输入区没有把换模式的事件转出去（选了模式但发送时还是旧模式）'],
  ['src/components/chat/ChatWindow.vue', ':modes="modes"', '聊天窗口没有把模式清单透给输入区'],
  ['src/components/chat/ChatWindow.vue', 'agentNameChange', '聊天窗口没有转发换模式事件'],
  ['src/views/StudyRoom.vue', 'const modes: ModeCard[]', 'StudyRoom 没有模式清单（模式选择器会空着）'],
  ['src/views/StudyRoom.vue', "value: ''", '模式清单里没有默认的「聊天」项（用户回不到聊天模式）'],
  ['src/views/StudyRoom.vue', '知识卡片', '「难点讲解」没有改名成「知识卡片」'],
  ['src/views/StudyRoom.vue', ":agent-name=\"selectedAgentName\"", 'StudyRoom 没有把当前模式传给聊天窗口'],
  ['src/views/StudyRoom.vue', 'modeExamples', '示例提问没有按模式分开（聊天界面挂着出题的示例）'],
  ['src/views/StudyRoom.vue', 'currentExamples', 'StudyRoom 没有把示例提问传下去'],
  // 工具栏顺序（第 26 轮）：模式 → 附件 → 知识库 →（右侧）发送。
  ['src/components/chat/ChatInput.vue', 'toolbar__mode', '模式选择器没有排到工具栏最前（用户要的顺序是 模式→附件→知识库）'],
  ['src/components/chat/ChatInput.vue', 'showExamples', '输入区没有渲染示例提问'],
  ['src/components/chat/ChatInput.vue', 'useExample', '示例提问点了没有反应（没有填进输入框）'],
  ['src/components/chat/ChatInput.vue', 'hasMessages', '示例提问没有"有聊天记录就不显示"的条件'],
  // 示例提问的位置（第 26 轮修正）：用户要求横向排在发送按钮旁边，不要再占输入框下方一整行。
  ['src/components/chat/ChatInput.vue', 'toolbar__right', '示例提问离开了工具栏右侧一组（会重新掉回输入框下方或跑到发送按钮右边）'],
  ['src/components/chat/ChatInput.vue', ':title="example"', '示例文字被省略号截断时没有悬停提示（用户看不到完整问题）'],
]
for (const [rel, needle, why] of wiring) {
  const target = path.join(process.cwd(), rel)
  const content = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : ''
  if (!content.includes(needle)) {
    failed++
    console.log(`WIRING FAIL ${rel}: 找不到 ${needle} —— ${why}`)
  }
}

// ---- 反向断言：这些东西**不能**出现在流式链路上 ----
// 正向断言只能证明"新写法在"，证明不了"旧写法走了"。ElLoading 全屏遮罩是最典型的
// 例子：它和流式增量同时存在时不会报任何错，只是把流式效果整个盖掉。
//
// 反向断言必须先**剥掉注释**：源码里的注释正是在解释"为什么不能这么写"，
// 把注释里出现的名字当成"用了它"会让这条断言永远红——那种误报会逼着后来人删掉解释。
function stripComments(src) {
  return src
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    // 行注释；前面的字符不能是 ':'，否则会把 https:// 这类 URL 从中间切断
    .replace(/(^|[^:])\/\/[^\n]*/gm, '$1')
}

const forbidden = [
  [
    'src/composables/useSessionStreamSender.ts',
    'ElLoading',
    '流式发送又用上了全屏 loading 遮罩：它会盖住流式文字，用户只能看到"整页加载中"',
  ],
  [
    'src/components/chat/MessageBubble.vue',
    'ElLoading',
    '气泡里不该用 ElLoading 表达流式状态（会盖住同一气泡内的增量文字）',
  ],
  // 第 26 轮下线/移除的入口：去掉的是 UI，后端能力都保留（归档仍由 A2A 自动接力触发）。
  // 这几条防的是"改别的东西时顺手把死控件又接回来"。
  [
    'src/views/StudyRoom.vue',
    'note_archiver',
    '「笔记归档」被挂回了模式清单：它现在只该由链路自动委派，不是用户可选的模式',
  ],
  [
    'src/components/chat/ChatInput.vue',
    'fetchMcpStatus',
    'MCP 指示器被挂回了输入区：本部署没有接入 MCP，它只会是一个永远不显示的死控件',
  ],
  [
    'src/components/chat/ChatInput.vue',
    'agent-option',
    '模式又变回了平铺按钮：模式清单要收在 ModePicker 里',
  ],
  // 删除文档（2026-10-20）：`ElMessageBox.confirm` 解析出来的是**动作字符串** `'confirm'`，
  // 不是 `{ value }`（那只有 `prompt` 才有）。曾经写成 `const confirmed = await ...;
  // if (!confirmed?.value) return` → 取值恒为 undefined → 点"确认删除"静默什么都不做，
  // 表现就是"知识库删除文档功能没做"。这条反向断言防的就是它被改回去。
  [
    'src/components/knowledge/KnowledgeDocumentReader.vue',
    'confirmed?.value',
    'ElMessageBox.confirm 取 .value 恒为 undefined：删除文档会静默 no-op（不报错也不删）',
  ],
]
for (const [rel, needle, why] of forbidden) {
  const target = path.join(process.cwd(), rel)
  const content = fs.existsSync(target) ? stripComments(fs.readFileSync(target, 'utf8')) : ''
  if (content.includes(needle)) {
    failed++
    console.log(`FORBIDDEN ${rel}: 不应出现 ${needle} —— ${why}`)
  }
}

console.log(`\nSFC checked: ${files.length}, failed: ${failed}`)
process.exit(failed ? 1 : 0)
