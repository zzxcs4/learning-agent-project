<script setup lang="ts">
import { computed } from 'vue'
import type { WeakPointItem, WeakPointList } from '@/api/profile'

/**
 * 弱点清单：活跃在前（出现次数降序），已改进/已归档收在折叠区。
 * 已毕业的弱点**不再进出题提示词**，所以默认折叠，避免干扰。
 */
const props = defineProps<{
  data: WeakPointList | null
  knowledgeBaseNames?: Record<number, string>
}>()

const active = computed(() => (props.data?.items ?? []).filter((item) => !item.improved && !item.archived))
const improved = computed(() => (props.data?.items ?? []).filter((item) => item.improved))
const archived = computed(() => (props.data?.items ?? []).filter((item) => item.archived))

const kbName = (item: WeakPointItem) => {
  if (!item.knowledge_base_id) return '通用'
  return props.knowledgeBaseNames?.[item.knowledge_base_id] ?? `库 #${item.knowledge_base_id}`
}
</script>

<template>
  <section class="profile-weak">
    <div class="profile-weak__head">
      <span class="profile-weak__title">薄弱点</span>
      <span class="profile-weak__hint">
        活跃 {{ data?.active ?? 0 }} · 已改进 {{ data?.improved ?? 0 }} · 已归档 {{ data?.archived ?? 0 }}
      </span>
    </div>

    <el-empty v-if="!active.length" description="还没有活跃的薄弱点（练几场、评测后就会沉淀出来）" />

    <div v-else class="profile-weak__list">
      <div v-for="item in active" :key="item.point" class="profile-weak__row">
        <div class="profile-weak__row-main">
          <el-tag size="small" type="warning" effect="light">{{ item.times_seen }} 次</el-tag>
          <span class="profile-weak__point">{{ item.point }}</span>
          <el-tag size="small" type="info" effect="plain">{{ kbName(item) }}</el-tag>
        </div>
        <div class="profile-weak__row-meta">
          <span>首次 {{ item.first_seen || '—' }}</span>
          <span>最近 {{ item.last_seen || '—' }}</span>
          <span v-if="item.absent_streak > 0" class="profile-weak__absent">
            连续 {{ item.absent_streak }} 场未复现
          </span>
        </div>
      </div>
    </div>

    <el-collapse v-if="improved.length || archived.length" class="profile-weak__collapsed">
      <el-collapse-item
        v-if="improved.length"
        :title="`已改进（${improved.length}）—— 连续 2 场未复现且分数达标`"
        name="improved">
        <div v-for="item in improved" :key="item.point" class="profile-weak__history-row">
          <span class="profile-weak__point">{{ item.point }}</span>
          <span class="profile-weak__history-meta">
            出现 {{ item.times_seen }} 次 · 最近 {{ item.last_seen || '—' }}
          </span>
        </div>
      </el-collapse-item>
      <el-collapse-item
        v-if="archived.length"
        :title="`已归档（${archived.length}）—— 连续 5 场未复现`"
        name="archived">
        <div v-for="item in archived" :key="item.point" class="profile-weak__history-row">
          <span class="profile-weak__point">{{ item.point }}</span>
          <span class="profile-weak__history-meta">
            出现 {{ item.times_seen }} 次 · 最近 {{ item.last_seen || '—' }}
          </span>
        </div>
      </el-collapse-item>
    </el-collapse>
  </section>
</template>

<style scoped lang="scss">
.profile-weak__head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}

.profile-weak__title {
  font-size: 15px;
  font-weight: 600;
  color: #30353e;
}

.profile-weak__hint {
  font-size: 12px;
  color: #8a9099;
}

.profile-weak__list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.profile-weak__row {
  padding: 12px 14px;
  border: 1px solid #ebedf0;
  border-radius: 10px;
}

.profile-weak__row-main {
  display: flex;
  align-items: center;
  gap: 10px;
}

.profile-weak__point {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  color: #30353e;
  word-break: break-word;
}

.profile-weak__row-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 8px;
  font-size: 12px;
  color: #8a9099;
}

.profile-weak__absent {
  color: #d9480f;
}

.profile-weak__collapsed {
  margin-top: 14px;
}

.profile-weak__history-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 0;
  font-size: 13px;
}

.profile-weak__history-meta {
  font-size: 12px;
  color: #8a9099;
  white-space: nowrap;
}
</style>
