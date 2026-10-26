<script setup lang="ts">
import ChatInput from './ChatInput.vue'
import MessageList from './MessageList.vue'
import type { ModeCard } from './ModePicker.vue'
import type { ChatSendPayload, UiMessage } from '@/types/chat'

defineProps<{
  messages: UiMessage[]
  hasMore?: boolean
  loadingMore?: boolean
  disabled?: boolean
  /** 候选模式（含默认的「聊天」），透传给输入区的模式选择器 */
  modes?: ModeCard[]
  /** 当前模式（undefined = 默认聊天模式）：单向传入，改动由 agentNameChange 回给父级 */
  agentName?: string
  /** 输入框下方的示例提问 */
  examples?: string[]
  /** 当前会话 id：知识库挂载是会话级的，需要透传给输入区（ChatInput） */
  sessionId?: string | number | null
}>()

const emit = defineEmits<{
  send: [payload: ChatSendPayload]
  attach: []
  loadMore: []
  quizSubmitted: []
  quizParamsConfirmed: [runId: string]
  /** 用户在模式选择器里换了模式 */
  agentNameChange: [value: string | undefined]
}>()
</script>

<template>
  <main class="chat-window">
    <MessageList
      :messages="messages"
      :has-more="hasMore"
      :loading-more="loadingMore"
      @load-more="emit('loadMore')"
      @quiz-submitted="emit('quizSubmitted')"
      @quiz-params-confirmed="emit('quizParamsConfirmed', $event)"
    />
    <ChatInput
      :disabled="disabled"
      :modes="modes"
      :agent-name="agentName"
      :examples="examples"
      :session-id="sessionId"
      :has-messages="messages.length > 0"
      @send="emit('send', $event)"
      @agent-name-change="emit('agentNameChange', $event)"
      @attach="emit('attach')" />
  </main>
</template>

<style scoped lang="scss">
.chat-window {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
</style>
