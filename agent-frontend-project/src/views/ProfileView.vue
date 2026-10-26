<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getDueReviews, getProfileMastery, getProfileStats, getProfileWeakPoints, type DueReviewList, type MasteryItem, type ProfileStats as ProfileStatsData, type WeakPointList } from '@/api/profile'
import { listPractices, type PracticeListData } from '@/api/practice'
import AppHeader from '@/components/layout/AppHeader.vue'
import PracticeHistory from '@/components/profile/PracticeHistory.vue'
import ProfileDueReviews from '@/components/profile/ProfileDueReviews.vue'
import ProfileMastery from '@/components/profile/ProfileMastery.vue'
import ProfileStats from '@/components/profile/ProfileStats.vue'
import ProfileWeakPoints from '@/components/profile/ProfileWeakPoints.vue'
import { tabs } from '@/composables/useRoomShell'

/**
 * 学习报告（画像页）：统计 / **今日到期复习** / 掌握度 / 薄弱点 / 练习历史。
 * 全部来自后端真实接口，**没有"知识覆盖率"**（该指标已明确不做）。
 */
const PAGE_SIZE = 10

const route = useRoute()
const router = useRouter()

const activeTab = ref('profile')
const loading = ref(false)
const stats = ref<ProfileStatsData | null>(null)
const masteryItems = ref<MasteryItem[]>([])
const weakPoints = ref<WeakPointList | null>(null)
const dueReviews = ref<DueReviewList | null>(null)
const practiceData = ref<PracticeListData | null>(null)
const practicePage = ref(1)
const practiceLoading = ref(false)

// 弱点里只存 knowledge_base_id，展示时用掌握度列表里的名字做映射
const knowledgeBaseNames = computed<Record<number, string>>(() => {
  const map: Record<number, string> = {}
  masteryItems.value.forEach((item) => {
    if (item.knowledge_base_name) map[item.knowledge_base_id] = item.knowledge_base_name
  })
  return map
})

const hasAnyData = computed(
  () =>
    Boolean(stats.value?.practice_count) ||
    masteryItems.value.length > 0 ||
    Boolean(weakPoints.value?.total) ||
    Boolean(dueReviews.value?.total),
)

const handleTabClick = async (tabKey: string) => {
  const selected = tabs.find((tab) => tab.key === tabKey)
  if (!selected) return
  activeTab.value = tabKey
  await router.push(selected.path)
}

const loadOverview = async () => {
  loading.value = true
  try {
    // 四个接口互不依赖，并行拉取；任一失败不影响其它区块展示
    const [statsResult, masteryResult, weakResult, dueResult] = await Promise.allSettled([
      getProfileStats(10),
      getProfileMastery(),
      getProfileWeakPoints(),
      getDueReviews(),
    ])
    stats.value = statsResult.status === 'fulfilled' ? statsResult.value : null
    masteryItems.value = masteryResult.status === 'fulfilled' ? masteryResult.value : []
    weakPoints.value = weakResult.status === 'fulfilled' ? weakResult.value : null
    dueReviews.value = dueResult.status === 'fulfilled' ? dueResult.value : null
  } finally {
    loading.value = false
  }
}

const loadPractices = async (page = 1) => {
  practiceLoading.value = true
  try {
    practiceData.value = await listPractices({ page, page_size: PAGE_SIZE })
    practicePage.value = page
  } finally {
    practiceLoading.value = false
  }
}

/** 练习记录里补答完成：本场得分与掌握度都可能变了，练习列表与画像统计一起刷新。 */
const refreshAfterPractice = async () => {
  await Promise.all([loadPractices(practicePage.value), loadOverview()])
}

onMounted(async () => {
  const tab = tabs.find((item) => item.path === route.path)
  if (tab) activeTab.value = tab.key
  await Promise.all([loadOverview(), loadPractices(1)])
})
</script>

<template>
  <div class="profile-room">
    <AppHeader :tabs="tabs" :active-tab="activeTab" @tab-click="handleTabClick" />

    <div v-loading="loading" class="profile-room__content">
      <div class="profile-room__inner">
        <ProfileStats :stats="stats" />

        <el-empty
          v-if="!loading && !hasAnyData"
          description="还没有学习数据：去学习室出几道题，评分后这里就会有掌握度与薄弱点" />

        <template v-else>
          <!-- 今日到期复习（SM-2）：放在最上面，因为它是"现在该做什么" -->
          <ProfileDueReviews
            v-if="dueReviews"
            class="profile-room__due"
            :data="dueReviews"
            :knowledge-base-names="knowledgeBaseNames" />

          <div class="profile-room__grid">
            <div class="profile-room__col">
              <ProfileMastery :items="masteryItems" />
            </div>
            <div class="profile-room__col">
              <ProfileWeakPoints :data="weakPoints" :knowledge-base-names="knowledgeBaseNames" />
            </div>
          </div>
        </template>

        <PracticeHistory
          class="profile-room__history"
          :data="practiceData"
          :loading="practiceLoading"
          :knowledge-base-names="knowledgeBaseNames"
          @page-change="loadPractices"
          @submitted="refreshAfterPractice" />
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.profile-room {
  width: 100%;
  height: 100vh;
  display: flex;
  flex-direction: column;
}

.profile-room__content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  /* 不再铺不透明米色：背景由全局 DynamicBackground 提供 */
  background: transparent;
  padding: 20px 24px 32px;
}

.profile-room__inner {
  width: min(1180px, 100%);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.profile-room__grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  align-items: start;
}

.profile-room__col {
  background: #ffffff;
  border: 1px solid #ebedf0;
  border-radius: 12px;
  padding: 16px;
}

.profile-room__history {
  background: #ffffff;
  border: 1px solid #ebedf0;
  border-radius: 12px;
  padding: 16px;
}

.profile-room__due {
  background: #ffffff;
  border: 1px solid #ebedf0;
  border-radius: 12px;
  padding: 16px;
}

@media (max-width: 1100px) {
  .profile-room__grid {
    grid-template-columns: 1fr;
  }
}
</style>
