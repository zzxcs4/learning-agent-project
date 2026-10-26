<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useUserStore } from '../../stores/user'
import { clearTokens } from '../../api/token'
import logoUrl from '../../assets/image/logo.png'

// --- 类型定义 ---
type NavTab = {
    key: string
    label: string
}

// --- Props & Emits ---
defineProps<{
    activeTab: string
    tabs: NavTab[]
}>()

const emit = defineEmits<{
    tabClick: [tabKey: string]
}>()

// --- 状态管理 ---
const router = useRouter()
const userStore = useUserStore()

// --- 事件处理 ---
function handleLogout() {
    clearTokens() // clearTokens 会自动调用 userStore.clearUser()
    router.replace('/login')
}
</script>

<template>
    <header class="app-header">
        <!-- 品牌 Logo -->
        <div class="brand">
            <img class="brand__logo" :src="logoUrl" alt="智学慧练" />
            <span class="brand__name">智学慧练</span>
        </div>

        <!-- 导航标签：只保留文字（参考图 2），下划线表示当前页 -->
        <nav class="nav-tabs">
            <button
                v-for="tab in tabs"
                :key="tab.key"
                type="button"
                class="tab-btn"
                :class="{ 'tab-btn--active': activeTab === tab.key }"
                @click="emit('tabClick', tab.key)">
                {{ tab.label }}
            </button>
        </nav>

        <!-- 右侧用户操作区 -->
        <div class="header-actions">
            <el-dropdown v-if="userStore.isLoggedIn">
                <div class="user-profile">
                    <el-avatar :size="36" :src="userStore.avatar || ''"> </el-avatar>
                    <span class="username">{{ userStore.username }}</span>
                </div>
                <template #dropdown>
                    <el-dropdown-menu>
                        <el-dropdown-item @click="handleLogout">退出登录</el-dropdown-item>
                    </el-dropdown-menu>
                </template>
            </el-dropdown>
        </div>
    </header>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as v;

.app-header {
    height: 72px;
    border-bottom: 1px solid v.$header-border;
    background-color: v.$header-bg;
    box-shadow: v.$header-shadow;
    padding: 0 20px;
    display: flex;
    align-items: center;
    gap: 30px;
    /* 压在全局背景之上 */
    position: relative;
    z-index: 10;
}

.header-actions {
    margin-left: auto;
}

.user-profile {
    display: flex;
    align-items: center;
    gap: 12px;
    cursor: pointer;
}

.username {
    font-size: 16px;
    font-weight: 600;
    color: #30353e;
}

.brand {
    display: flex;
    align-items: center;
    gap: 10px;
}

.brand__logo {
    width: 34px;
    height: 34px;
    display: block;
    flex-shrink: 0;
}

.brand__name {
    font-size: 18px;
    line-height: 1;
    font-weight: 700;
    color: #30353e;
    letter-spacing: 0.2px;
    white-space: nowrap;
}

/* 导航标签：纯文字，当前页用下划线标记（参考图 2） */
.nav-tabs {
    display: flex;
    align-items: center;
    gap: 34px;
    height: 100%;
}

.tab-btn {
    position: relative;
    height: 100%;
    padding: 0 2px;
    border: none;
    background: transparent;
    color: #6b7280;
    font-size: 17px;
    font-weight: 600;
    cursor: pointer;
    transition: color 0.16s ease;

    &:hover {
        color: #1f1f1f;
    }

    &.tab-btn--active {
        color: #1f1f1f;
        font-weight: 700;

        &::after {
            content: '';
            position: absolute;
            left: 0;
            right: 0;
            bottom: 18px;
            height: 3px;
            border-radius: 2px;
            background: #000000;
        }
    }
}
</style>
