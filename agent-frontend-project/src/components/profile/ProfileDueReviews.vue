<script setup lang="ts">
import { computed } from 'vue'
import type { DueReviewItem, DueReviewList } from '@/api/profile'

/**
 * 今日到期复习（SM-2 调度，升级规划 §八 第 3 期）：画像页里"今天该复习什么"。
 *
 * 与旁边「薄弱点」的分工：薄弱点是**全部沉淀**（含已改进/已归档），
 * 这里是**今天的动作**——只含活跃且 `next_review <= 今天` 的条目，
 * 拖得越久的排前面（同日的按出现次数降序，后端已排好序）。
 */
const props = defineProps<{
  data: DueReviewList | null
  knowledgeBaseNames?: Record<number, string>
}>()

const items = computed<DueReviewItem[]>(() => props.data?.items ?? [])

const kbName = (item: DueReviewItem) => {
  if (!item.knowledge_base_id) return '通用'
  return props.knowledgeBaseNames?.[item.knowledge_base_id] ?? `库 #${item.knowledge_base_id}`
}
</script>

<template>
  <section class="profile-due">
    <div class="profile-due__head">
      <span class="profile-due__title">今日到期复习</span>
      <span class="profile-due__hint">
        到期 {{ data?.total ?? 0 }} 条 · 判定日 {{ data?.today || '—' }}
      </span>
    </div>

    <el-empty
      v-if="!items.length"
      description="今天没有到期复习（新弱点排在明天，复现的弱点会归零重排）" />

    <div v-else class="profile-due__list">
      <div v-for="item in items" :key="item.point" class="profile-due__row">
        <div class="profile-due__row-main">
          <el-tag size="small" type="danger" effect="light">{{ item.times_seen }} 次</el-tag>
          <span class="profile-due__point">{{ item.point }}</span>
          <el-tag size="small" type="info" effect="plain">{{ kbName(item) }}</el-tag>
        </div>
        <div class="profile-due__row-meta">
          <span>应复习 {{ item.next_review }}</span>
          <span>间隔 {{ item.interval_days }} 天</span>
          <span>难度因子 {{ item.ease_factor.toFixed(2) }}</span>
          <span v-if="item.last_seen">最近出现 {{ item.last_seen }}</span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped lang="scss">
.profile-due__head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}

.profile-due__title {
  font-size: 15px;
  font-weight: 600;
  color: #30353e;
}

.profile-due__hint {
  font-size: 12px;
  color: #8a9099;
}

.profile-due__list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.profile-due__row {
  padding: 12px 14px;
  border: 1px solid #e4e7ed;
  border-radius: 10px;
  background: #f7f8fa;
}

.profile-due__row-main {
  display: flex;
  align-items: center;
  gap: 10px;
}

.profile-due__point {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  color: #30353e;
  word-break: break-word;
}

.profile-due__row-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 8px;
  font-size: 12px;
  color: #8a9099;
}
</style>
