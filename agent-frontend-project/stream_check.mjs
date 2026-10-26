// 流式显示行为门禁（第 12 轮）。
//
// 跑法：node stream_check.mjs   （必须放在 agent-frontend-project 下，需要 node_modules/esbuild）
//
// 为什么要有这个脚本：这一轮修的是"回车后整页 loading 遮罩把流式文字盖住"。
// 这类 bug **编译能过、类型能过、SFC 能过、接线断言也能过**——
// 静态检查最多证明"源码里没有 ElLoading 了"，证明不了"回车之后气泡里的字真的在长"。
// 所以这里用 esbuild 把**真实的 `useSessionStreamSender`** 打成 node 模块，
// 喂一段脚本化的 SSE 事件序列，断言"阶段状态"与"增量文字"的时序关系。
//
// 关键不变式（回归时最先看的几条）：
//   1. 首帧就有 streaming 状态（loop 在第一个 delta 之前要跑好几秒）；
//   2. 每类事件都把阶段文案推进到对应步骤；
//   3. 增量文字**在 streaming 仍为 true 时**就开始追加（否则就是"最后一次性出现"的老毛病）；
//   4. 结束/报错/被打断三条路径都必须把 streaming 归位，不能留一个永远转的状态。
import { build } from 'esbuild'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const root = process.cwd()
const tmpBundle = path.join(root, '.tmp-stream-check.mjs')
const tmpStub = path.join(root, '.tmp-stream-stub.mjs')

let failed = 0
let passed = 0
const check = (cond, label, detail) => {
  if (cond) {
    passed++
    console.log(`  ok   ${label}`)
  } else {
    failed++
    console.log(`  FAIL ${label}${detail ? ` —— ${detail}` : ''}`)
  }
}
const section = (title) => console.log(`\n[${title}]`)

// 被替换掉的 `@/api/streamChat`：按 `globalThis.__STREAM_SCRIPT__` 逐条回放事件。
// `__before__` 钩子用来在指定的两条事件之间插入动作（例如中途 stopActiveStream）。
const STUB_SOURCE = `
export const AGENT_LOOP_MODE = 'agent_loop'
export async function streamChat(options) {
  const script = globalThis.__STREAM_SCRIPT__ || []
  for (const event of script) {
    if (typeof event.__before__ === 'function') await event.__before__()
    const payload = { ...event }
    delete payload.__before__
    options.onEvent(payload)
  }
  return globalThis.__STREAM_RESULT__
}
`

const ENTRY_SOURCE = `
import { useSessionStreamSender } from './src/composables/useSessionStreamSender'
export function createSender(options) {
  return useSessionStreamSender(options)
}
`

/** `@/xxx` → `src/xxx`；`@/api/streamChat` 单独换成桩。 */
const aliasPlugin = {
  name: 'dsh-alias',
  setup(b) {
    b.onResolve({ filter: /^@\/api\/streamChat$/ }, () => ({ path: tmpStub }))
    b.onResolve({ filter: /^@\// }, (args) => {
      const rel = args.path.slice(2)
      for (const suffix of ['.ts', '.js', '/index.ts']) {
        const candidate = path.join(root, 'src', rel + suffix)
        if (fs.existsSync(candidate)) return { path: candidate }
      }
      return { path: path.join(root, 'src', rel) }
    })
  },
}

/** 打一次包，返回真实的 composable 工厂。 */
async function loadComposable() {
  fs.writeFileSync(tmpStub, STUB_SOURCE, 'utf8')
  const result = await build({
    stdin: { contents: ENTRY_SOURCE, resolveDir: root, loader: 'ts' },
    bundle: true,
    write: false,
    format: 'esm',
    platform: 'node',
    target: 'node20',
    logLevel: 'silent',
    plugins: [aliasPlugin],
  })
  fs.writeFileSync(tmpBundle, result.outputFiles[0].text, 'utf8')
  return import(pathToFileURL(tmpBundle).href)
}

/** 造一个把 writer 回调全部记下来的假 createPending。 */
function makeHarness() {
  const state = {
    statuses: [],
    deltas: '',
    reasoning: '',
    /** 每追加一段增量时，当时最新的阶段状态——用来证明"文字在长"和"状态在跑"是同时的 */
    statusAtDelta: [],
    finished: null,
    traces: [],
    errors: [],
    syncCalls: 0,
  }
  const createPending = () => ({
    appendDelta: (delta) => {
      state.deltas += delta
      state.statusAtDelta.push(state.statuses[state.statuses.length - 1] ?? null)
    },
    appendReasoning: (delta) => {
      state.reasoning += delta
    },
    finish: (fullText, sources) => {
      state.finished = { fullText, sources }
    },
    onTrace: (trace) => state.traces.push(trace),
    onStatus: (status) => state.statuses.push({ ...status }),
  })
  return { state, createPending }
}

const buildSender = (composable, state, createPending, overrides = {}) =>
  composable.createSender({
    sessionModel: 0,
    getActiveSessionId: () => 7,
    createPending,
    syncAfterDone: async () => {
      state.syncCalls += 1
    },
    onError: (message) => state.errors.push(message),
    ...overrides,
  })

const lastStatus = (state) => state.statuses[state.statuses.length - 1] ?? null
const statusTexts = (state) => state.statuses.filter((s) => s.streaming).map((s) => s.text)

const main = async () => {
  const composable = await loadComposable()

  // ---- 场景 1：完整 agent loop（start → step → tool_call → tool_result → step → delta → done）----
  section('完整 loop：阶段文案与增量文字同时推进')
  {
    const { state, createPending } = makeHarness()
    globalThis.__STREAM_SCRIPT__ = [
      { type: 'start', message_id: 1, mode: 'agent_loop' },
      { type: 'step', index: 1, kind: 'think' },
      { type: 'tool_call', name: 'search_material', args: { query: '极限' }, step: 1 },
      { type: 'tool_result', name: 'search_material', ok: true, elapsed_ms: 128, result_preview: '命中 3 段' },
      { type: 'step', index: 2, kind: 'think' },
      { type: 'delta', text: '极' },
      { type: 'delta', text: '限' },
      { type: 'delta', text: '的定义是…' },
      {
        type: 'done',
        response_text: '极限的定义是…',
        sources: [{ file_name: 'a.pdf', score: 0.9 }],
        stop_reason: 'final_answer',
        trace: {
          type: 'agent_loop_trace',
          stop_reason: 'final_answer',
          steps: [
            { index: 1, kind: 'tool', tool_calls: ['search_material'] },
            { index: 2, kind: 'final' },
          ],
          tool_calls: [{ name: 'search_material', ok: true, elapsed_ms: 128 }],
        },
      },
    ]
    globalThis.__STREAM_RESULT__ = undefined
    const sender = buildSender(composable, state, createPending)
    await sender.send('什么是极限')

    // 1. 首帧状态：必须在任何事件之前就有（loop 的第一个 delta 可能好几秒后才来）
    check(
      state.statuses[0]?.streaming === true && state.statuses[0]?.text === '学习搭档正在处理…',
      '回车后立即出现内联阶段状态（不是等第一个 token）',
      JSON.stringify(state.statuses[0]),
    )
    const texts = statusTexts(state)
    check(texts.includes('正在推理…'), 'step 事件把状态推进到「正在推理…」', texts.join(' | '))
    check(
      texts.includes('正在调用 检索知识库…'),
      'tool_call 事件带上工具可读名（不是裸 search_material）',
      texts.join(' | '),
    )
    check(
      texts.includes('检索知识库 已返回，继续推理…'),
      'tool_result 事件反映"已返回"',
      texts.join(' | '),
    )
    // 2. 「正在作答…」只写一次：每个 delta 都改状态会让文案闪烁
    check(
      texts.filter((t) => t === '正在作答…').length === 1,
      '「正在作答…」只在首个 delta 写一次（不随每个 token 抖动）',
      `出现 ${texts.filter((t) => t === '正在作答…').length} 次`,
    )
    // 3. 核心不变式：增量文字是在 streaming 仍为 true 的时候写进去的
    check(state.deltas === '极限的定义是…', '增量文字逐段拼接完整', JSON.stringify(state.deltas))
    check(
      state.statusAtDelta.length === 3 && state.statusAtDelta.every((s) => s && s.streaming === true),
      '每段增量追加之时的状态都是 streaming=true（文字与状态同时可见，不是最后一次性出现）',
      JSON.stringify(state.statusAtDelta),
    )
    check(
      state.finished?.fullText === '极限的定义是…',
      'done 事件用服务端全文兜底',
      JSON.stringify(state.finished),
    )
    check(state.traces.length > 0, 'step/tool_call/tool_result 都推进了实时轨迹')
    const lastTrace = state.traces[state.traces.length - 1]
    check(
      lastTrace?.stop_reason === 'final_answer' && lastTrace?.tool_calls?.length === 1,
      'done 带回权威轨迹（含 stop_reason）覆盖实时快照',
      JSON.stringify(lastTrace),
    )
    // SSE 路径 streamChat 返回 undefined，`if (result && ...)` 会跳过 syncAfterDone。
    // 这是**有意**的：done 事件已经带来权威全文 + 来源 + 轨迹，再回刷一次只是多一个
    // 网络往返，还会用落库版本替换掉实时轨迹。回刷只服务于"返回 JSON 的工作流"（见场景 6）。
    check(
      state.syncCalls === 0,
      'SSE 路径不回刷（done 已带来权威数据，少一次往返）',
      `syncAfterDone 调用 ${state.syncCalls} 次`,
    )
    // 4. 收尾：状态必须归位，否则气泡永远留着"正在…"
    check(
      lastStatus(state)?.streaming === false && lastStatus(state)?.text === '',
      '流结束后状态归位（气泡撤掉提示与光标）',
      JSON.stringify(lastStatus(state)),
    )
    check(state.errors.length === 0, '正常路径不报错', state.errors.join(' | '))
  }

  // ---- 场景 2：被打断（切会话 / 新建 / 删会话）----
  section('中途被打断：状态立即归位且不再写入')
  {
    const { state, createPending } = makeHarness()
    const holder = {}
    globalThis.__STREAM_SCRIPT__ = [
      { type: 'start', message_id: 2, mode: 'agent_loop' },
      { type: 'delta', text: '开头' },
      {
        type: 'delta',
        text: '不该出现',
        __before__: () => {
          // 模拟用户在流式期间切走会话
          holder.sender.stopActiveStream()
        },
      },
    ]
    globalThis.__STREAM_RESULT__ = undefined
    holder.sender = buildSender(composable, state, createPending)
    await holder.sender.send('会被打断的问题')

    check(
      state.deltas === '开头',
      '打断之后的增量不再写入（epoch 守卫仍生效）',
      JSON.stringify(state.deltas),
    )
    check(
      lastStatus(state)?.streaming === false,
      'stopActiveStream 立即把状态归位（不用等 fetch 真正返回）',
      JSON.stringify(state.statuses.map((s) => `${s.streaming}:${s.text}`)),
    )
  }

  // ---- 场景 3：错误事件 ----
  section('错误路径：状态归位并上报')
  {
    const { state, createPending } = makeHarness()
    globalThis.__STREAM_SCRIPT__ = [
      { type: 'start', message_id: 3, mode: 'agent_loop' },
      { type: 'error', message: '模型调用失败' },
    ]
    globalThis.__STREAM_RESULT__ = undefined
    const sender = buildSender(composable, state, createPending)
    await sender.send('会失败的问题')
    check(state.errors[0] === '模型调用失败', 'error 事件透传给 onError', state.errors.join(' | '))
    check(
      lastStatus(state)?.streaming === false && lastStatus(state)?.text === '',
      '错误路径同样把状态归位',
      JSON.stringify(lastStatus(state)),
    )
  }

  // ---- 场景 4：委派与 MCP 工具的可读名 ----
  section('委派 / MCP 工具在阶段文案里的可读名')
  {
    const { state, createPending } = makeHarness()
    globalThis.__STREAM_SCRIPT__ = [
      { type: 'start', message_id: 4, mode: 'agent_loop' },
      { type: 'tool_call', name: 'delegate_quiz', step: 1 },
      { type: 'tool_result', name: 'delegate_quiz', ok: false, result_preview: '超时' },
      { type: 'tool_call', name: 'mcp_fetch_fetch', step: 2 },
      { type: 'done', response_text: '完', stop_reason: 'final_answer' },
    ]
    globalThis.__STREAM_RESULT__ = undefined
    const sender = buildSender(composable, state, createPending)
    await sender.send('委派与 MCP')

    const texts = statusTexts(state)
    check(texts.includes('正在调用 委派：出题…'), '委派工具显示中文名', texts.join(' | '))
    check(
      texts.includes('委派：出题 未成功，继续推理…'),
      '失败的委派在状态里明说"未成功"（不是静默）',
      texts.join(' | '),
    )
    check(
      texts.includes('正在调用 fetch_fetch…'),
      'MCP 工具去掉 mcp_ 前缀（server_tool 原样保留，不猜下划线归属）',
      texts.join(' | '),
    )
  }

  // ---- 场景 5：非 loop 链路（无 trace 的普通流式）----
  section('普通流式：没有轨迹时也必须给内联状态')
  {
    const { state, createPending } = makeHarness()
    globalThis.__STREAM_SCRIPT__ = [
      { type: 'start', message_id: 5 },
      { type: 'delta', text: '直接回答' },
      { type: 'done', response_text: '直接回答' },
    ]
    globalThis.__STREAM_RESULT__ = undefined
    const sender = buildSender(composable, state, createPending)
    await sender.send('普通问题')
    check(state.statuses[0]?.streaming === true, '普通链路首帧也有状态')
    check(
      statusTexts(state).includes('正在作答…'),
      '普通链路首个 delta 切到「正在作答…」',
      statusTexts(state).join(' | '),
    )
    check(state.traces.length === 0, '没有轨迹事件时不产生空轨迹（不该渲染空轨迹面板）')
    check(lastStatus(state)?.streaming === false, '普通链路收尾同样归位')
  }

  // ---- 场景 6：工作流返回 JSON（非 SSE）----
  section('JSON 工作流：必须回刷（前端手上没有任何权威文本）')
  {
    const { state, createPending } = makeHarness()
    // 没有任何事件：JSON 响应不会走 readStream，也就永远不会有 done。
    globalThis.__STREAM_SCRIPT__ = []
    globalThis.__STREAM_RESULT__ = { route_type: 'workflow', status: 'ok', data: {} }
    const sender = buildSender(composable, state, createPending)
    await sender.send('出题')
    check(state.syncCalls === 1, 'JSON 路径回刷一次（否则气泡永远空着）', `调用 ${state.syncCalls} 次`)
    check(
      lastStatus(state)?.streaming === false && lastStatus(state)?.text === '',
      'JSON 路径也把内联状态归位',
      JSON.stringify(lastStatus(state)),
    )
  }

  // ---- 场景 7：空轨迹不该被推给气泡 ----
  section('空轨迹：普通回答下面不该挂「执行轨迹 · 0 步」')
  {
    const { state, createPending } = makeHarness()
    globalThis.__STREAM_SCRIPT__ = [{ type: 'start', message_id: 7 }, { type: 'delta', text: '嗨' }]
    globalThis.__STREAM_RESULT__ = undefined
    const sender = buildSender(composable, state, createPending)
    await sender.send('你好')
    check(
      state.traces.length === 0,
      '没有 step/tool_call 的 done 不推空轨迹',
      `推了 ${state.traces.length} 次：${JSON.stringify(state.traces)}`,
    )
  }

  // ---- 场景 8：思考型模型的 reasoning 增量 ----
  section('思考内容：单独累积、不污染正文、不额外刷状态')
  {
    const { state, createPending } = makeHarness()
    globalThis.__STREAM_SCRIPT__ = [
      { type: 'start', message_id: 8, mode: 'agent_loop' },
      { type: 'step', index: 1, kind: 'think' },
      { type: 'reasoning', text: '先想一下' },
      { type: 'reasoning', text: '用户问的是索引' },
      { type: 'delta', text: '索引是' },
      { type: 'delta', text: '一种数据结构。' },
      { type: 'done', response_text: '索引是一种数据结构。', stop_reason: 'final_answer' },
    ]
    globalThis.__STREAM_RESULT__ = undefined
    const sender = buildSender(composable, state, createPending)
    await sender.send('什么是索引')

    check(
      state.reasoning === '先想一下用户问的是索引',
      'reasoning 事件逐片累积到独立缓冲',
      JSON.stringify(state.reasoning),
    )
    check(
      state.deltas === '索引是一种数据结构。',
      '正文只包含 delta，**没有**混进思考内容',
      JSON.stringify(state.deltas),
    )
    check(
      !state.deltas.includes('先想一下') && !state.deltas.includes('用户问的是索引'),
      '思考内容没有泄漏到正文',
      JSON.stringify(state.deltas),
    )
    check(
      state.finished?.fullText === '索引是一种数据结构。',
      'done 的权威全文也不含思考内容',
      JSON.stringify(state.finished),
    )
    // 状态文案：step 已经置「正在推理…」，reasoning 不该再切一个同义文案（会闪）
    const texts = statusTexts(state)
    check(
      texts.filter((t) => t === '正在推理…').length >= 1 && !texts.includes('正在思考…'),
      'reasoning 不额外改阶段文案（避免同义文案闪烁）',
      texts.join(' | '),
    )
    check(
      statusTexts(state).includes('正在作答…'),
      '正文开始后仍切到「正在作答…」',
      texts.join(' | '),
    )
    check(lastStatus(state)?.streaming === false, '思考型链路收尾同样归位')
  }

  // ---- 场景 9：agent loop 里委派出题（第 22 轮）----
  // 出题消息是出题 worker 自己落库的（request_id = "{run_id}:quiz"），SSE 路径按设计不回刷，
  // 所以"出了题"必须单独回刷一次，否则界面上根本没有那条题目消息，答题弹窗也就无从弹起。
  section('agent loop 委派出题：出了题才回刷一次（普通问答仍然零回刷）')
  {
    const { state, createPending } = makeHarness()
    const quizCalls = []
    globalThis.__STREAM_SCRIPT__ = [
      { type: 'start', message_id: 9, mode: 'agent_loop' },
      { type: 'step', index: 1, kind: 'think' },
      { type: 'tool_call', name: 'delegate_quiz', step: 1 },
      {
        type: 'tool_result',
        name: 'delegate_quiz',
        ok: true,
        run_id: 'run-abc',
        result_preview: '已生成 5 题',
      },
      { type: 'step', index: 2, kind: 'think' },
      { type: 'delta', text: '题目出好了' },
      { type: 'done', response_text: '题目出好了', stop_reason: 'final_answer' },
    ]
    globalThis.__STREAM_RESULT__ = undefined
    const sender = buildSender(composable, state, createPending, {
      onQuizGenerated: async (sessionId, runIds) => {
        quizCalls.push({ sessionId, runIds: [...runIds] })
      },
    })
    await sender.send('出点题练练')

    check(quizCalls.length === 1, '委派出题后回刷一次消息列表', `回刷 ${quizCalls.length} 次`)
    check(
      quizCalls[0]?.runIds?.length === 1 && quizCalls[0]?.runIds?.[0] === 'run-abc',
      '回刷带回 run_id（前端靠它只认领本轮新出的题）',
      JSON.stringify(quizCalls[0]?.runIds),
    )
    check(quizCalls[0]?.sessionId === 7, '回刷指向当前会话', String(quizCalls[0]?.sessionId))
    check(state.syncCalls === 0, '出题回刷走专用回调，不误触发同步回刷', `syncAfterDone ${state.syncCalls} 次`)
    check(
      state.deltas === '题目出好了' && lastStatus(state)?.streaming === false,
      '出题链路自身照常收尾（文字落位、状态归位）',
      JSON.stringify(state.deltas),
    )
  }

  // ---- 场景 10：出题委派失败 / 没带 run_id 时不该回刷 ----
  section('出题委派没成功：不回刷（没有新消息可认领）')
  {
    const { state, createPending } = makeHarness()
    const quizCalls = []
    globalThis.__STREAM_SCRIPT__ = [
      { type: 'start', message_id: 10, mode: 'agent_loop' },
      { type: 'tool_call', name: 'delegate_quiz', step: 1 },
      { type: 'tool_result', name: 'delegate_quiz', ok: false, result_preview: '出题超时' },
      { type: 'done', response_text: '出题失败了，要再试一次吗', stop_reason: 'final_answer' },
    ]
    globalThis.__STREAM_RESULT__ = undefined
    const sender = buildSender(composable, state, createPending, {
      onQuizGenerated: async (sessionId, runIds) => {
        quizCalls.push({ sessionId, runIds: [...runIds] })
      },
    })
    await sender.send('出点题')

    check(quizCalls.length === 0, '失败的委派不触发回刷', `回刷 ${quizCalls.length} 次`)
    check(state.errors.length === 0, '委派失败不等于请求失败（不该弹错误提示）')
  }

  console.log(`\n流式显示断言：${passed} 通过 / ${failed} 失败`)
  return failed
}
let code = 1
try {
  code = await main()
} catch (err) {
  console.log(`\n脚本异常：${err && err.stack ? err.stack : err}`)
  code = 1
} finally {
  for (const file of [tmpBundle, tmpStub]) {
    try {
      fs.unlinkSync(file)
    } catch {
      // 临时文件可能没生成，忽略
    }
  }
}
process.exit(code ? 1 : 0)
