<script setup lang="ts">
import { Plus } from '@element-plus/icons-vue'
import HistoryList from '@/components/history/HistoryList.vue'

type SessionItem = {
  id: number | string
  title: string
}

// 文案可定制：学习室叫"会话"，笔记本叫"笔记"
withDefaults(
  defineProps<{
    sessions: SessionItem[]
    activeSessionId: number | string
    createLabel?: string
    sectionTitle?: string
  }>(),
  {
    createLabel: '新建会话',
    sectionTitle: '历史会话',
  }
)

const emit = defineEmits<{
  createSession: []
  sessionClick: [id: number | string]
  editSession: [id: number | string]
  removeSession: [id: number | string]
}>()
</script>

<template>
  <aside class="side-bar">
    <!-- 侧栏只负责触发事件，具体业务由各页面视图处理。 -->
    <el-button class="create-btn" @click="emit('createSession')">
      <el-icon><Plus /></el-icon>
      {{ createLabel }}
    </el-button>
    <p class="section-title">{{ sectionTitle }}</p>
    <HistoryList
      :sessions="sessions"
      :active-session-id="activeSessionId"
      @session-click="emit('sessionClick', $event)"
      @edit-session="emit('editSession', $event)"
      @remove-session="emit('removeSession', $event)"
    />
  </aside>
</template>

<style scoped lang="scss">
@use '@/styles/variables' as v;

.side-bar {
  width: 320px;
  border-right: 1px solid rgba(0, 0, 0, 0.06);
  /* 半透明白：让全局淡紫背景透出来，同时保证列表文字可读 */
  background-color: rgba(255, 255, 255, 0.55);
  padding: 18px 16px 22px 16px;
  display: flex;
  flex-direction: column;
}

.create-btn {
  width: 100%;
  height: 48px;
  border: none;
  border-radius: 6px;
  background: v.$primary-btn-bg;
  color: #ffffff;
  font-size: 16px;
  font-weight: 600;
  margin-top: 4px;
  margin-bottom: 20px;

  &:hover,
  &:focus-visible {
    background: v.$primary-btn-hover;
    color: #ffffff;
  }

  &:active {
    background: v.$primary-btn-active;
    color: #ffffff;
  }
}

.section-title {
  color: #4d525a;
  font-size: 18px;
  font-weight: 700;
  margin: 0 0 14px;
}
</style>
