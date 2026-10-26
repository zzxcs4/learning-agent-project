<!--
  DynamicBackground.vue
  全局背景：白底之上叠若干柔和的淡紫色光晕（对应参考项目 DynamicBackground.vue 的 decor-element）。
  注意：只保留参考图里的装饰光晕，**不包含**三角形 / 圆形 / 方形等几何图形。
-->
<template>
  <div class="dynamic-background" aria-hidden="true">
    <div class="decor-element decor-1"></div>
    <div class="decor-element decor-2"></div>
    <div class="decor-element decor-3"></div>
    <div class="decor-element decor-4"></div>
    <div class="decor-element decor-5"></div>
  </div>
</template>

<style scoped lang="scss">
.dynamic-background {
  position: fixed;
  inset: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  pointer-events: none;
  /* 放在所有页面内容之下。
     这里**刻意不设底色**：底色交给 App.vue 的 .app-shell（半透明白）——
     若在这里铺白底，白底与光晕同处一个 z-index:-1 的层叠上下文里，
     光晕会画在白底之上、又把页面内容盖住。 */
  z-index: -1;
}

.decor-element {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  will-change: transform;
}

/* 左上：紫罗兰主光晕 */
.decor-1 {
  width: 620px;
  height: 620px;
  background: #a78bfa;
  top: -300px;
  left: -160px;
  opacity: 0.38;
  animation: float 20s infinite ease-in-out;
}

/* 中上：淡紫补光，呼应参考图中部的浅紫过渡 */
.decor-2 {
  width: 560px;
  height: 560px;
  background: #c4b5fd;
  top: -260px;
  left: 30%;
  opacity: 0.26;
  animation: float 26s infinite ease-in-out 3s;
}

/* 右侧：洋红/玫紫 */
.decor-3 {
  width: 540px;
  height: 540px;
  background: #c147e9;
  top: 20%;
  right: -180px;
  opacity: 0.24;
  animation: float 24s infinite ease-in-out 2s;
}

/* 右下：暖桃色，对应参考图右下的淡橘光晕 */
.decor-4 {
  width: 460px;
  height: 460px;
  background: #f6b48c;
  bottom: -180px;
  right: 12%;
  opacity: 0.2;
  animation: float 30s infinite ease-in-out 6s;
}

/* 左下：淡紫，压住左下角空白 */
.decor-5 {
  width: 420px;
  height: 420px;
  background: #b39ddb;
  bottom: -200px;
  left: -80px;
  opacity: 0.24;
  animation: float 22s infinite ease-in-out 4s;
}

@keyframes float {
  0%,
  100% {
    transform: translate3d(0, 0, 0) scale(1);
  }
  50% {
    transform: translate3d(0, -26px, 0) scale(1.06);
  }
}

@media (prefers-reduced-motion: reduce) {
  .decor-element {
    animation: none;
  }
}
</style>
