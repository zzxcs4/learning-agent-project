<script setup lang="ts">
import { Delete, Edit } from '@element-plus/icons-vue'
import type { Component } from 'vue'

type SessionItem = {
  id: number | string
  title: string
}

defineProps<{
  session: SessionItem
  active: boolean
  icon: Component
}>()

const emit = defineEmits<{
  click: [id: number | string]
  edit: [id: number | string]
  remove: [id: number | string]
}>()
</script>

<template>
  <button class="history-item" :class="{ 'history-item--active': active }" type="button" @click="emit('click', session.id)">
    <el-icon class="history-item__icon">
      <component :is="icon" />
    </el-icon>
    <span class="history-item__title">{{ session.title }}</span>
    <span class="history-item__actions">
      <el-icon class="action" @click.stop="emit('edit', session.id)"><Edit /></el-icon>
      <el-icon class="action action--danger" @click.stop="emit('remove', session.id)"><Delete /></el-icon>
    </span>
  </button>
</template>

<style scoped lang="scss">
.history-item {
  height: 44px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #515762;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 12px;
  cursor: pointer;
  transition: all 0.15s ease;
  text-align: left;

  &:hover {
    background: #eceff3;
  }
}

.history-item--active {
  background: #e6edf7;
  color: #2f4660;
}

.history-item__icon {
  font-size: 15px;
}

.history-item__title {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 16px;
  font-weight: 500;
}

.history-item__actions {
  display: flex;
  gap: 8px;
  color: #8f99ab;
}

.action:hover {
  color: #3f6791;
}

.action--danger:hover {
  color: #c14c40;
}
</style>
