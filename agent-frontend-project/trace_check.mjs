// 轨迹解析闸门：验证 normalizeAgentTrace / traceFromResponsePayload 的防御性与标签映射。
// 这些是纯函数，却决定了 AgentTrace 会不会因为一条异常历史数据而渲染崩掉。
//
// 用 esbuild 把真实的 `src/utils/agentTrace.ts` 转成 ESM 再动态 import ——
// **测的就是会被打包进产物的那份源码**，不是复制品。
//
// 跑法（agent-frontend-project 下）：node trace_check.mjs
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const sourceFile = path.join(process.cwd(), 'src/utils/agentTrace.ts')
const source = fs.readFileSync(sourceFile, 'utf8')
const { code } = await esbuild.transform(source, { loader: 'ts', format: 'esm', target: 'es2022' })

// 类型 import 在 transform 后已被移除，直接写进临时文件再 import
const tempFile = path.join(os.tmpdir(), `agentTrace-check-${process.pid}.mjs`)
fs.writeFileSync(tempFile, code, 'utf8')

let mod
try {
  mod = await import(pathToFileURL(tempFile).href)
} catch (err) {
  console.log(`加载 agentTrace.ts 失败：${err.message}`)
  process.exit(1)
} finally {
  fs.rmSync(tempFile, { force: true })
}

const {
  formatToolArgs,
  isMcpCall,
  isWorkerCall,
  normalizeAgentTrace,
  toolLabel,
  traceFromResponsePayload,
  traceSummary,
} = mod

let failed = 0
let passed = 0
const check = (name, cond, extra = '') => {
  if (cond) {
    passed += 1
    console.log(`  PASS ${name}`)
  } else {
    failed += 1
    console.log(`  FAIL ${name} ${extra}`)
  }
}

console.log('[1] 落库结构回放')
const payload = {
  type: 'agent_loop_trace',
  stop_reason: 'final_answer',
  steps: [
    { index: 1, kind: 'tool', elapsed_ms: 12, tool_calls: ['get_weak_points'], text_preview: '' },
    { index: 2, kind: 'final', elapsed_ms: 340, tool_calls: [], text_preview: '答案' },
  ],
  tool_calls: [
    {
      name: 'get_weak_points',
      args: { limit: 3 },
      ok: false,
      elapsed_ms: 18,
      result_preview: '{"error": "boom"}',
    },
  ],
}
const trace = traceFromResponsePayload(payload)
check('能识别 agent_loop_trace', trace !== null)
check('步骤数正确', trace?.steps.length === 2, String(trace?.steps.length))
check('stop_reason 保留', trace?.stop_reason === 'final_answer')
check('工具参数保留', trace?.tool_calls?.[0]?.args?.limit === 3, JSON.stringify(trace?.tool_calls))
// 第 3 期补的字段：没有它们，"工具失败"在回放里和成功长得一模一样
check('工具成败保留', trace?.tool_calls?.[0]?.ok === false, JSON.stringify(trace?.tool_calls?.[0]))
check('工具耗时保留', trace?.tool_calls?.[0]?.elapsed_ms === 18)
check('结果预览保留', trace?.tool_calls?.[0]?.result_preview === '{"error": "boom"}')
check('每步模型文本预览保留', trace?.steps?.[1]?.text_preview === '答案')

console.log('[2] 非轨迹载荷必须返回 null（否则单轮聊天会显示空轨迹）')
check('普通聊天 payload → null', traceFromResponsePayload({ type: 'learning_quiz' }) === null)
check('null → null', traceFromResponsePayload(null) === null)
check('无步骤无工具 → null', normalizeAgentTrace({ type: 'agent_loop_trace', steps: [], tool_calls: [] }) === null)

console.log('[3] 脏数据不能抛异常')
check('steps 不是数组 → 不崩且为 null', normalizeAgentTrace({ steps: 'oops', tool_calls: [] }) === null)
check(
  '元素是 null → 被过滤',
  traceFromResponsePayload({
    type: 'agent_loop_trace',
    steps: [null, { index: 1, kind: 'think' }],
    tool_calls: [null],
  })?.steps.length === 1,
)
check(
  '缺 name 的 tool_call → 被过滤',
  normalizeAgentTrace({ type: 'agent_loop_trace', steps: [{ index: 1 }], tool_calls: [{ args: {} }] })
    ?.tool_calls.length === 0,
)
check(
  '字符串耗时 → 转数字',
  normalizeAgentTrace({ type: 'agent_loop_trace', steps: [{ index: 1, elapsed_ms: '25' }], tool_calls: [] })
    ?.steps[0]?.elapsed_ms === 25,
)
check(
  '脏的 ok（字符串）不会当布尔用',
  normalizeAgentTrace({
    type: 'agent_loop_trace',
    steps: [{ index: 1 }],
    tool_calls: [{ name: 'x', ok: 'yes', result_preview: 42 }],
  })?.tool_calls[0]?.ok === undefined,
)
check(
  '脏的 result_preview（非字符串）被丢弃',
  normalizeAgentTrace({
    type: 'agent_loop_trace',
    steps: [{ index: 1 }],
    tool_calls: [{ name: 'x', result_preview: { a: 1 } }],
  })?.tool_calls[0]?.result_preview === undefined,
)

console.log('[4] 标签与分类')
check('已知工具给中文名', toolLabel('search_material') === '检索知识库', toolLabel('search_material'))
check('未知工具原样显示（不隐藏信息）', toolLabel('some_new_tool') === 'some_new_tool')
check('delegate_* 判定为委派', isWorkerCall('delegate_quiz') === true)
check('普通工具不是委派', isWorkerCall('get_mastery') === false)
check('参数渲染可读', formatToolArgs({ limit: 3 }) === 'limit=3', formatToolArgs({ limit: 3 }))
const longArgs = formatToolArgs({ query: 'x'.repeat(200) })
check('超长参数值被截断到 60 字 + 省略号', longArgs === `query=${'x'.repeat(60)}…`, `len=${longArgs.length}`)
check('汇总文案含工具次数与步数', traceSummary(trace).includes('1 次工具/委派'), traceSummary(trace))

console.log('[5] MCP 工具（第 3 期）')
check('mcp_ 前缀判定为 MCP', isMcpCall('mcp_self-kb_search_material') === true)
check('内置工具不是 MCP', isMcpCall('search_material') === false)
check(
  'MCP 工具只去掉前缀、不拆 server/tool（server 名本身可能带下划线）',
  toolLabel('mcp_self-kb_search_material') === 'self-kb_search_material',
  toolLabel('mcp_self-kb_search_material'),
)
check(
  '下划线 server 名不会被拆错',
  toolLabel('mcp_my_server_get_weather') === 'my_server_get_weather',
  toolLabel('mcp_my_server_get_weather'),
)
check('MCP 不是委派', isWorkerCall('mcp_self-kb_get_mastery') === false)
check(
  '汇总文案标出 MCP 次数',
  traceSummary({
    type: 'agent_loop_trace',
    steps: [{ index: 1 }],
    tool_calls: [{ name: 'mcp_a_t' }, { name: 'get_mastery' }],
  }).includes('含 1 次 MCP'),
  traceSummary({ type: 'agent_loop_trace', steps: [{ index: 1 }], tool_calls: [{ name: 'mcp_a_t' }] }),
)
check(
  '汇总文案标出失败次数（别让失败藏在"跑完了"里）',
  traceSummary({
    type: 'agent_loop_trace',
    steps: [{ index: 1 }],
    tool_calls: [{ name: 'search_material', ok: false }, { name: 'get_mastery', ok: true }],
  }).includes('1 次失败'),
)

console.log(`\n结果：${failed ? `${failed} 项失败` : '全部通过'}（断言 ${passed}）`)
process.exit(failed ? 1 : 0)
