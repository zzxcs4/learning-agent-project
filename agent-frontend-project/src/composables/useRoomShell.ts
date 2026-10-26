import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

export type NavTab = {
  key: string
  label: string
  path: string
}

export type SessionItem = {
  id: string
  title: string
}

const baseSessions: SessionItem[] = [
  { id: 's-1', title: 'Vue 3 组合式 API 学习' },
  { id: 's-2', title: '数据库优化策略' },
  { id: 's-3', title: '系统架构设计' },
  { id: 's-4', title: '云计算基础' },
  { id: 's-5', title: '移动端开发要点' },
  { id: 's-6', title: '网络安全防护' },
  { id: 's-7', title: '数据分析方法' },
  { id: 's-8', title: '机器学习入门' }
]

export const tabs: NavTab[] = [
  { key: 'study-room', label: '学习室', path: '/study-room' },
  { key: 'knowledge-base', label: '知识库', path: '/knowledge-base' },
  { key: 'notebook', label: '笔记本', path: '/notebook' },
  { key: 'profile', label: '学习报告', path: '/profile' }
]

export const useRoomShell = (createNamePrefix = '新会话') => {
  const router = useRouter()
  const route = useRoute()

  const activeTab = ref('study-room')
  const activeSessionId = ref(baseSessions[0]?.id ?? '')
  const sessionCounter = ref(baseSessions.length + 1)
  const sessions = ref<SessionItem[]>([...baseSessions])

  watch(
    () => route.path,
    (path) => {
      const tab = tabs.find((item) => item.path === path)
      if (tab) {
        activeTab.value = tab.key
      }
    },
    { immediate: true }
  )

  const handleTabClick = async (tabKey: string) => {
    const selectedTab = tabs.find((tab) => tab.key === tabKey)
    if (!selectedTab) {
      return
    }
    activeTab.value = tabKey
    await router.push(selectedTab.path)
  }

  const handleSessionClick = (id: string) => {
    activeSessionId.value = id
  }

  const handleCreateSession = () => {
    const sessionId = `s-${sessionCounter.value}`
    sessions.value.unshift({
      id: sessionId,
      title: `${createNamePrefix} ${sessionCounter.value}`
    })
    activeSessionId.value = sessionId
    sessionCounter.value += 1
    return sessionId
  }

  return {
    activeTab,
    activeSessionId,
    sessions,
    tabs,
    handleCreateSession,
    handleSessionClick,
    handleTabClick
  }
}
