
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { getMyProfile, type UserProfile } from '@/api/user' // 路径使用 @ 别名，更规范
import { clearTokens } from '@/api/token'
import router from '@/router'

export const useUserStore = defineStore('user', () => {
  // --- state ---
  const profile = ref<UserProfile | null>(null)

  // --- getters ---
  const isLoggedIn = computed(() => !!profile.value)
  const username = computed(() => profile.value?.username || ''
  )
  const avatar = computed(() => profile.value?.avatar || '')

  // --- actions ---

  /**
   * 从后端获取当前用户信息，并更新 state
   */
  async function fetchUser() {
    const userProfile = await getMyProfile()
    if (userProfile) {
      profile.value = userProfile
    } else {
      // 获取失败，可能 token 失效，清理状态
      clearUser()
    }
  }

  /**
   * 更新用户头像
   * @param newAvatarUrl 新头像的 URL
   */
  function updateUserAvatar(newAvatarUrl: string) {
    if (profile.value) {
      profile.value.avatar = newAvatarUrl
    }
  }

  /**
   * 清除用户信息和 token（用于登出）
   */
  function clearUser() {
    profile.value = null
    // 清理本地存储的 token
    clearTokens()
    // 可选：重定向到登录页
    router.push('/login')
  }

  return {
    profile,
    isLoggedIn,
    username,
    avatar,
    fetchUser,
    updateUserAvatar,
    clearUser
  }
})

