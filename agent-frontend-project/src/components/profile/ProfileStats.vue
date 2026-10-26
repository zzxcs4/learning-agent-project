<script setup lang="ts">
import { computed } from 'vue'
import type { ProfileStats } from '@/api/profile'

/**
 * 画像页顶部：四个统计 + 最近 N 场分数趋势。
 * 趋势不引 echarts（省一个依赖），用等高柱状块 + tooltip 表达。
 */
const props = defineProps<{
  stats: ProfileStats | null
}>()

const cards = computed(() => {
  const stats = props.stats
  if (!stats) return []
  return [
    { label: '总练习次数', value: String(stats.practice_count), unit: '场' },
    { label: '已完成评分', value: String(stats.completed_count), unit: '场' },
    { label: '平均分', value: stats.completed_count ? stats.average_score.toFixed(1) : '—', unit: '' },
    { label: '最近一次', value: stats.latest_score === null ? '—' : String(stats.latest_score), unit: '' },
  ]
})

// 柱高按 100 分制折算，最低留 4% 让 0 分也可见
const barHeight = (score: number) => `${Math.max(4, Math.min(100, score))}%`

const latestAt = computed(() => {
  const ts = props.stats?.latest_practice_at
  if (!ts) return '还没有练习记录'
  return `最近一次练习：${new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })}`
})
</script>

<template>
  <section class="profile-stats">
    <div class="profile-stats__cards">
      <div v-for="card in cards" :key="card.label" class="profile-stats__card">
        <span class="profile-stats__label">{{ card.label }}</span>
        <span class="profile-stats__value">
          {{ card.value }}<span v-if="card.unit" class="profile-stats__unit">{{ card.unit }}</span>
        </span>
      </div>
    </div>

    <div class="profile-stats__trend">
      <div class="profile-stats__trend-head">
        <span class="profile-stats__section-title">最近得分趋势</span>
        <span class="profile-stats__hint">{{ latestAt }}</span>
      </div>
      <div v-if="stats?.recent_scores?.length" class="profile-stats__bars">
        <div
          v-for="(score, index) in stats.recent_scores"
          :key="index"
          class="profile-stats__bar-wrap"
          :title="`第 ${index + 1} 场：${score} 分`">
          <span class="profile-stats__bar-score">{{ score }}</span>
          <div class="profile-stats__bar" :style="{ height: barHeight(score) }" />
        </div>
      </div>
      <p v-else class="profile-stats__empty">完成一次练习后这里会出现趋势</p>
    </div>
  </section>
</template>

<style scoped lang="scss">
.profile-stats__cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
}

.profile-stats__card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 18px;
  border-radius: 12px;
  background: #f7f8fa;
}

.profile-stats__label {
  font-size: 13px;
  color: #8a9099;
}

.profile-stats__value {
  font-size: 28px;
  font-weight: 700;
  color: #30353e;
  line-height: 1;
}

.profile-stats__unit {
  margin-left: 4px;
  font-size: 13px;
  font-weight: 400;
  color: #8a9099;
}

.profile-stats__trend {
  margin-top: 18px;
  padding: 16px 18px;
  border-radius: 12px;
  background: #f7f8fa;
}

.profile-stats__trend-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 14px;
}

.profile-stats__section-title {
  font-size: 15px;
  font-weight: 600;
  color: #30353e;
}

.profile-stats__hint {
  font-size: 12px;
  color: #8a9099;
}

.profile-stats__bars {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  height: 120px;
}

.profile-stats__bar-wrap {
  flex: 1;
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: center;
  gap: 6px;
}

.profile-stats__bar-score {
  font-size: 12px;
  color: #6b7280;
}

.profile-stats__bar {
  width: 100%;
  max-width: 42px;
  border-radius: 6px 6px 2px 2px;
  background: linear-gradient(180deg, #4a4a4a 0%, #000000 100%);
}

.profile-stats__empty {
  margin: 0;
  font-size: 13px;
  color: #8a9099;
}
</style>
