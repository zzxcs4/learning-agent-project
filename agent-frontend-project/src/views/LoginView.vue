<template>
  <div class="login-page">
    <LoginCard />
  </div>
</template>

<script setup lang="ts">
import LoginCard from '../components/LoginCard.vue'
</script>

<style scoped lang="scss">
.login-page {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  /* 背景由全局 DynamicBackground + App.vue 的 .app-shell 提供 */
  background: transparent;
  padding: 40px 16px;
}
</style>
