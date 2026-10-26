<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import MessageBubble from './MessageBubble.vue'
import type { UiMessage } from '@/types/chat'

const props = defineProps<{
  messages: UiMessage[]
  hasMore?: boolean
  loadingMore?: boolean
}>()

const emit = defineEmits<{
  loadMore: []
  quizSubmitted: []
  /** 用户确认了出题参数：父级要回刷消息（参数卡会被原地换成题目） */
  quizParamsConfirmed: [runId: string]
}>()

const listRef = ref<HTMLElement | null>(null)
// 顶部加载更多时记录滚动位置，等数据插入后做高度补偿，避免列表“跳动”。
const pendingTopCompensation = ref<{ scrollTop: number; scrollHeight: number } | null>(null)
const requestedDuringLoading = ref(false)

// 「贴底」状态：只有用户本来就在底部时，流式增量才自动跟着滚。
// 之前只在 messages.length 变化时滚一次，而流式期间长度是不变的（同一条消息的 text 在变长），
// 于是回复越写越长、越写越靠下，最后一段永远在可视区外——看上去像"没在流式"。
const NEAR_BOTTOM_PX = 120
const stickToBottom = ref(true)

const isNearBottom = () => {
  const el = listRef.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX
}

const scrollToBottom = () => {
  if (!listRef.value) {
    return
  }
  listRef.value.scrollTop = listRef.value.scrollHeight
}

const onScroll = () => {
  if (!listRef.value) {
    return
  }
  // 先记录贴底状态，再做分页判断（分页的早退分支不该让贴底状态失效）。
  stickToBottom.value = isNearBottom()
  if (!props.hasMore || props.loadingMore) {
    return
  }
  // 距离顶部 100px 触发上拉分页；loading 锁由 props.loadingMore 控制。
  if (listRef.value.scrollTop > 100 || requestedDuringLoading.value) {
    return
  }
  pendingTopCompensation.value = {
    scrollTop: listRef.value.scrollTop,
    scrollHeight: listRef.value.scrollHeight
  }
  requestedDuringLoading.value = true
  emit('loadMore')
}

// 最后一条消息：流式期间它的 text / 轨迹会持续增长，是"内容变高"的唯一来源。
const lastMessage = computed(() => props.messages[props.messages.length - 1])

watch(
  () => [
    props.messages.length,
    lastMessage.value?.text.length ?? 0,
    lastMessage.value?.trace?.tool_calls.length ?? 0,
    lastMessage.value?.streaming ? 1 : 0
  ],
  async () => {
    if (props.loadingMore) {
      return
    }
    await nextTick()
    if (pendingTopCompensation.value && listRef.value) {
      const { scrollTop, scrollHeight } = pendingTopCompensation.value
      // 插入旧消息后恢复视觉位置：新高度 - 旧高度 = 需要补偿的偏移。
      const delta = listRef.value.scrollHeight - scrollHeight
      listRef.value.scrollTop = scrollTop + delta
      pendingTopCompensation.value = null
      requestedDuringLoading.value = false
      return
    }
    // 用户主动往上翻看历史时不抢滚动条，否则每来一段增量就被拽回底部。
    if (stickToBottom.value) {
      scrollToBottom()
    }
  },
  { immediate: true }
)
</script>

<template>
  <section ref="listRef" class="message-list" @scroll="onScroll">
    <div class="message-list__inner">
      <p v-if="props.loadingMore" class="loading-hint">正在加载更多消息...</p>
      <MessageBubble
        v-for="message in props.messages"
        :key="message.id"
        :message="message"
        @quiz-submitted="emit('quizSubmitted')"
        @quiz-params-confirmed="emit('quizParamsConfirmed', $event)" />
    </div>
  </section>
</template>

<style scoped lang="scss">
.message-list {
  flex: 1;
  overflow: auto;
  padding: 26px 24px 20px;
  /* 透明：让全局淡紫背景透出来（原来是米色 #f0ece2） */
  background: transparent;
}

.message-list__inner {
  width: min(1040px, 100%);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.loading-hint {
  margin: 0 0 12px;
  font-size: 13px;
  color: #7b8290;
}
</style>
