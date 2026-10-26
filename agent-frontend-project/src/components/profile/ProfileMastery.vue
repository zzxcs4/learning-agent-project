<script setup lang="ts">
import { zoneTagType, type MasteryItem } from '@/api/profile'

/**
 * 按知识库的掌握度：每条一个进度条 + 分区标签。
 * 掌握度的粒度就是"知识库"（键 = knowledge_base_id），所以这里一行代表一个库。
 */
defineProps<{
  items: MasteryItem[]
}>()

const progressColor = (zone: string) => {
  if (zone === '优势区') return '#2f9e44'
  if (zone === '过渡区') return '#f6c700'
  return '#e03131'
}

const formatTime = (ts: number | null) => {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}
</script>

<template>
  <section class="profile-mastery">
    <div class="profile-mastery__head">
      <span class="profile-mastery__title">按知识库的掌握度</span>
      <span class="profile-mastery__hint">分区阈值：补课区 &lt; 40 · 过渡区 40–69 · 优势区 ≥ 70</span>
    </div>

    <el-empty v-if="!items.length" description="还没有掌握度记录，先在某个知识库上练一次" />

    <div v-else class="profile-mastery__list">
      <div v-for="item in items" :key="item.knowledge_base_id" class="profile-mastery__row">
        <div class="profile-mastery__row-head">
          <span class="profile-mastery__name" :title="item.knowledge_base_name || ''">
            {{ item.knowledge_base_name || `知识库 #${item.knowledge_base_id}（已删除）` }}
          </span>
          <div class="profile-mastery__row-meta">
            <el-tag size="small" :type="zoneTagType(item.zone)" effect="light">{{ item.zone }}</el-tag>
            <span class="profile-mastery__score">{{ item.mastery_score.toFixed(1) }} 分</span>
          </div>
        </div>
        <el-progress
          :percentage="Math.min(100, Math.max(0, item.mastery_score))"
          :stroke-width="10"
          :show-text="false"
          :color="progressColor(item.zone)" />
        <div class="profile-mastery__row-foot">
          <span>已练 {{ item.practice_count }} 场</span>
          <span>最近评测：{{ formatTime(item.last_assessed_at) }}</span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped lang="scss">
.profile-mastery__head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}

.profile-mastery__title {
  font-size: 15px;
  font-weight: 600;
  color: #30353e;
}

.profile-mastery__hint {
  font-size: 12px;
  color: #8a9099;
}

.profile-mastery__list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.profile-mastery__row {
  padding: 14px 16px;
  border: 1px solid #ebedf0;
  border-radius: 10px;
}

.profile-mastery__row-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.profile-mastery__name {
  font-size: 14px;
  font-weight: 600;
  color: #30353e;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.profile-mastery__row-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.profile-mastery__score {
  font-size: 13px;
  color: #6b7280;
}

.profile-mastery__row-foot {
  display: flex;
  justify-content: space-between;
  margin-top: 8px;
  font-size: 12px;
  color: #8a9099;
}
</style>
