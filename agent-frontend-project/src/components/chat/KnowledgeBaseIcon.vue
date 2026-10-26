<script setup lang="ts">
/**
 * 知识库图标：数据库圆柱体（第 24 轮，按用户给的参考图）。
 *
 * 为什么不从 `@element-plus/icons-vue` 拿：那套图标里没有"数据库"这个形状
 * （最接近的 Coin/Box 都不对），而为了一个图标引第三方图标包不值得。
 * 内联 SVG 还能直接吃 `currentColor`——按钮禁用/选中态的颜色自动跟随，不用写两套。
 */
withDefaults(defineProps<{ size?: number }>(), { size: 16 })
</script>

<template>
    <svg
        class="kb-icon"
        :width="size"
        :height="size"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.7"
        stroke-linecap="round"
        aria-hidden="true">
        <!-- 顶面椭圆的"盖子" -->
        <ellipse cx="12" cy="5.6" rx="7.4" ry="2.9" />
        <!-- 圆柱侧壁（到底部再收一个圆弧） -->
        <path d="M4.6 5.6v12.3c0 1.6 3.31 2.9 7.4 2.9s7.4-1.3 7.4-2.9V5.6" />
        <!-- 中间那道"分层"弧线 -->
        <path d="M4.6 11.6c0 1.6 3.31 2.9 7.4 2.9s7.4-1.3 7.4-2.9" />
    </svg>
</template>

<style scoped lang="scss">
.kb-icon {
    display: block;
    flex-shrink: 0;
}
</style>
