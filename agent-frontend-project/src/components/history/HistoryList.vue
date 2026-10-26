<script setup lang="ts">
import {
  Cellphone,
  Coin,
  Connection,
  DataLine,
  Lock,
  Monitor,
  Notebook,
  Opportunity
} from '@element-plus/icons-vue'
import type { Component } from 'vue'
import HistoryItem from './HistoryItem.vue'

type SessionItem = {
  id: number | string
  title: string
}

const props = defineProps<{
  sessions: SessionItem[]
  activeSessionId: number | string
}>()

const emit = defineEmits<{
  sessionClick: [id: number | string]
  editSession: [id: number | string]
  removeSession: [id: number | string]
}>()

const iconMap: Component[] = [Monitor, Coin, Connection, Opportunity, Cellphone, Lock, DataLine, Notebook]

const getIcon = (index: number): Component => iconMap[index % iconMap.length] ?? Monitor
</script>

<template>
  <div class="history-list">
    <HistoryItem
      v-for="(session, index) in props.sessions"
      :key="session.id"
      :session="session"
      :icon="getIcon(index)"
      :active="session.id === props.activeSessionId"
      @click="emit('sessionClick', $event)"
      @edit="emit('editSession', $event)"
      @remove="emit('removeSession', $event)"
    />
  </div>
</template>

<style scoped lang="scss">
.history-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  overflow: auto;
  padding-right: 4px;
  padding-left: 4px;
}
</style>
