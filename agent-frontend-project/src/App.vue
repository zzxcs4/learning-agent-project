<script setup lang="ts">
import { onMounted } from 'vue'
import { useUserStore } from './stores/user'
import { getAccessToken } from './api/token'
import DynamicBackground from './components/DynamicBackground.vue'

// 在应用根组件挂载时，尝试从本地存储恢复用户状态
onMounted(() => {
  const userStore = useUserStore()
  const token = getAccessToken()

  // 如果存在 token，就从后端获取用户信息
  if (token) {
    userStore.fetchUser()
  }
})
</script>

<template>
  <!-- 全局背景：白底 + 淡紫色光晕，垫在所有路由页面之下（同参考项目 App.vue 的做法）。
       各页面外壳用半透明白做柔和叠加，不能再用不透明底色，否则会把光晕整片盖住。 -->
  <div class="app-shell">
    <DynamicBackground />
    <router-view />
  </div>
</template>

<style scoped lang="scss">
.app-shell {
  position: relative;
  min-height: 100vh;
  /* 半透明白叠加在光晕之上，得到参考图那种"淡淡的紫"而不是纯白 */
  background: rgba(255, 255, 255, 0.5);
}
</style>
