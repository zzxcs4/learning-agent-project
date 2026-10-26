<script setup lang="ts">
import { computed, ref } from 'vue'
import { formatToolArgs, isMcpCall, isWorkerCall, toolLabel, traceSummary } from '@/utils/agentTrace'
import type { AgentRunTrace, AgentToolCall } from '@/types/chat'

/**
 * Agent 执行轨迹时间线。
 *
 * 两个数据来源共用这一个组件（形状已由 `utils/agentTrace.ts` 收敛）：
 * - **实时**：流式期间由 `useSessionStreamSender` 累积 step / tool_call / tool_result；
 * - **回放**：刷新页面后从消息的 `responsePayload` 读回。
 *
 * 交互取舍：
 * - 正在执行时**默认展开**（用户需要看到"它在干活"，否则会以为卡住）；
 * - 执行结束后**默认折叠**（答案才是主体，轨迹退居次要），仍可点开复查。
 *
 * 明细里除了名字，还给出**参数、耗时、成功与否、结果预览**：
 * `ToolRuntime` 会把工具异常收敛成"给模型看的观察结果"，所以"调用没报错"不等于"工具成功了"——
 * 只显示名字的话，用户根本分不出"没查到资料"和"工具炸了"。
 */
const props = defineProps<{
  trace: AgentRunTrace
  /** 仍在流式执行中；为真时默认展开并显示进行态 */
  running?: boolean
}>()

// 用户是否手动切换过折叠状态；没切换时跟随 running 自动变化
const manualOpen = ref<boolean | null>(null)

const isOpen = computed(() => manualOpen.value ?? Boolean(props.running))

const toggle = () => {
  manualOpen.value = !isOpen.value
}

const summary = computed(() => traceSummary(props.trace))

const toolCalls = computed<AgentToolCall[]>(() => props.trace.tool_calls)

/** 用「工具名 + 第几次出现」生成稳定 key，避免同名工具多次调用时 key 冲突。 */
const toolCallKeys = computed(() => {
  const seen = new Map<string, number>()
  return toolCalls.value.map((call) => {
    const count = (seen.get(call.name) ?? 0) + 1
    seen.set(call.name, count)
    return `${call.name}-${count}`
  })
})

/** 展开过结果预览的调用（key 同上）：默认只显示两行，长结果按需展开。 */
const expandedResults = ref<Set<string>>(new Set())

// 模板里直接写 `toolCallKeys[idx]` 在 `noUncheckedIndexedAccess` 下是 `string | undefined`，
// 过不了 `Set.has()` 的类型检查（第 1 期坑 #13 的同源问题）。统一走这两个小函数。
const keyOf = (idx: number): string => toolCallKeys.value[idx] ?? `call-${idx}`

const isResultExpanded = (idx: number): boolean => expandedResults.value.has(keyOf(idx))

const toggleResult = (idx: number) => {
  const key = keyOf(idx)
  const next = new Set(expandedResults.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedResults.value = next
}

/** 结果预览是否需要"展开/收起"（短的就直接显示，不摆无用的按钮）。 */
const isLongPreview = (text?: string) => (text?.length ?? 0) > 120

/** 徽标文案：外部 MCP / 委派 / 内置工具三态，颜色也分开。 */
const callBadge = (name: string) => (isMcpCall(name) ? 'MCP' : isWorkerCall(name) ? '委派' : '工具')

const callBadgeClass = (name: string) => {
  if (isMcpCall(name)) return 'agent-trace__badge--mcp'
  return isWorkerCall(name) ? 'agent-trace__badge--worker' : 'agent-trace__badge--tool'
}

const stepLabel = (kind?: string) => {
  if (kind === 'tool') return '调用工具'
  if (kind === 'final') return '给出回答'
  return '推理中'
}

const formatElapsed = (value?: number | null) => {
  if (value === null || value === undefined) return ''
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${value}ms`
}
</script>

<template>
  <div class="agent-trace" :class="{ 'agent-trace--running': running }">
    <button type="button" class="agent-trace__head" @click="toggle">
      <span class="agent-trace__icon">{{ running ? '⏳' : '🧭' }}</span>
      <span class="agent-trace__title">
        {{ running ? '执行中' : '执行轨迹' }}
      </span>
      <span class="agent-trace__summary">{{ summary }}</span>
      <span class="agent-trace__chevron">{{ isOpen ? '收起' : '展开' }}</span>
    </button>

    <div v-if="isOpen" class="agent-trace__body">
      <!-- 步骤时间线 -->
      <ol class="agent-trace__steps">
        <li
          v-for="(step, idx) in trace.steps"
          :key="`step-${idx}`"
          class="agent-trace__step">
          <span class="agent-trace__dot" :class="`agent-trace__dot--${step.kind || 'think'}`" />
          <div class="agent-trace__step-main">
            <div class="agent-trace__step-line">
              <span class="agent-trace__step-label">
                第 {{ step.index ?? idx + 1 }} 步 · {{ stepLabel(step.kind) }}
              </span>
              <span v-if="step.elapsed_ms !== undefined" class="agent-trace__elapsed">
                {{ formatElapsed(step.elapsed_ms) }}
              </span>
            </div>
            <!-- 这一步模型说了什么（后端截断到 200 字符）：让"它在想什么"可见 -->
            <p v-if="step.text_preview" class="agent-trace__step-text">
              {{ step.text_preview }}
            </p>
            <div v-if="step.tool_calls?.length" class="agent-trace__tags">
              <span v-for="name in step.tool_calls" :key="name" class="agent-trace__tag">
                {{ toolLabel(name) }}
              </span>
            </div>
          </div>
        </li>
      </ol>

      <!-- 工具 / 委派明细 -->
      <div v-if="toolCalls.length" class="agent-trace__tools">
        <div class="agent-trace__tools-title">工具与委派明细</div>
        <div
          v-for="(call, idx) in toolCalls"
          :key="keyOf(idx)"
          class="agent-trace__tool"
          :class="{ 'agent-trace__tool--failed': call.ok === false }">
          <div class="agent-trace__tool-head">
            <span class="agent-trace__badge" :class="callBadgeClass(call.name)">
              {{ callBadge(call.name) }}
            </span>
            <span class="agent-trace__tool-name">{{ toolLabel(call.name) }}</span>
            <span v-if="call.ok === false" class="agent-trace__tool-state agent-trace__tool-state--bad">
              失败
            </span>
            <span v-else-if="call.ok === true" class="agent-trace__tool-state">成功</span>
            <span v-if="call.elapsed_ms !== undefined" class="agent-trace__elapsed">
              {{ formatElapsed(call.elapsed_ms) }}
            </span>
          </div>
          <!-- 参数：没有它就只能看到"检索知识库"，看不到"检索了什么" -->
          <div v-if="formatToolArgs(call.args)" class="agent-trace__tool-args">
            {{ formatToolArgs(call.args) }}
          </div>
          <!-- 结果预览：成败之外还要能看出"它到底查到了什么/为什么失败" -->
          <div v-if="call.result_preview" class="agent-trace__tool-result">
            <p
              class="agent-trace__tool-preview"
              :class="{
                'agent-trace__tool-preview--clamped':
                  isLongPreview(call.result_preview) && !isResultExpanded(idx),
              }">
              {{ call.result_preview }}
            </p>
            <button
              v-if="isLongPreview(call.result_preview)"
              type="button"
              class="agent-trace__tool-toggle"
              @click="toggleResult(idx)">
              {{ isResultExpanded(idx) ? '收起结果' : '展开结果' }}
            </button>
          </div>
        </div>
      </div>

      <p v-if="trace.stop_reason === 'max_steps'" class="agent-trace__warn">
        步数已用尽（8 步）仍未收敛，上面是当前结论；可以把问题拆得更具体再试一次。
      </p>
    </div>
  </div>
</template>

<style scoped lang="scss">
.agent-trace {
  margin-top: 10px;
  border: 1px solid #d8dce4;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.55);
  font-size: 14px;
  color: #526173;
  overflow: hidden;
}

.agent-trace--running {
  border-color: #b9cdf0;
  background: rgba(67, 133, 241, 0.06);
}

.agent-trace__head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 0;
  background: transparent;
  cursor: pointer;
  font-size: 14px;
  color: #4385f1;
  text-align: left;
}

.agent-trace__title {
  font-weight: 500;
}

.agent-trace__summary {
  color: #7b8290;
  font-size: 13px;
}

.agent-trace__chevron {
  margin-left: auto;
  color: #7b8290;
  font-size: 13px;
}

.agent-trace__body {
  padding: 4px 12px 12px;
  border-top: 1px solid #e4e8ef;
}

.agent-trace__steps {
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.agent-trace__step {
  display: flex;
  gap: 10px;
  padding: 6px 0;
}

.agent-trace__dot {
  width: 9px;
  height: 9px;
  margin-top: 6px;
  border-radius: 50%;
  background: #c3cad6;
  flex-shrink: 0;
}

.agent-trace__dot--tool {
  background: #4385f1;
}

.agent-trace__dot--final {
  background: #35b37e;
}

.agent-trace__step-main {
  flex: 1;
  min-width: 0;
}

.agent-trace__step-line {
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.agent-trace__step-label {
  color: #3f4856;
}

.agent-trace__elapsed {
  color: #9ea6b4;
  font-size: 13px;
}

.agent-trace__step-text {
  margin: 4px 0 0;
  color: #7b8290;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-word;
}

.agent-trace__tags {
  margin-top: 4px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.agent-trace__tag {
  padding: 1px 8px;
  border-radius: 10px;
  background: #e8ecf4;
  color: #526173;
  font-size: 13px;
}

.agent-trace__tools {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed #e4e8ef;
}

.agent-trace__tools-title {
  color: #7b8290;
  font-size: 13px;
  margin-bottom: 6px;
}

.agent-trace__tool {
  display: block;
  padding: 6px 0;
}

/* 失败的调用整行标出来：它不该和成功看起来一样 */
.agent-trace__tool--failed {
  border-left: 2px solid #e5797b;
  padding-left: 8px;
  margin-left: -2px;
}

.agent-trace__tool-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.agent-trace__tool-state {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 12px;
  background: #e6f6ee;
  color: #1f8a5b;
  flex-shrink: 0;
}

.agent-trace__tool-state--bad {
  background: #fdecec;
  color: #c0392b;
}

.agent-trace__tool-args {
  margin-top: 3px;
  color: #7b8290;
  font-size: 13px;
  word-break: break-word;
}

.agent-trace__tool-result {
  margin-top: 4px;
}

.agent-trace__tool-preview {
  margin: 0;
  padding: 6px 8px;
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.035);
  color: #526173;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.agent-trace__tool-preview--clamped {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.agent-trace__tool-toggle {
  margin-top: 4px;
  padding: 0;
  border: 0;
  background: transparent;
  color: #4385f1;
  font-size: 13px;
  cursor: pointer;
}

.agent-trace__badge {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 12px;
  flex-shrink: 0;
}

.agent-trace__badge--tool {
  background: #e8ecf4;
  color: #4385f1;
}

.agent-trace__badge--worker {
  background: #e6f6ee;
  color: #1f8a5b;
}

/* MCP：外部能力，单独一个颜色，一眼能和内置工具分开 */
.agent-trace__badge--mcp {
  background: #f1e9fb;
  color: #7a4fd0;
}

.agent-trace__tool-name {
  color: #3f4856;
  word-break: break-word;
}

.agent-trace__warn {
  margin: 8px 0 0;
  color: #b88230;
  font-size: 13px;
}
</style>
