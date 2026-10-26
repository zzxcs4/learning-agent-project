<script setup lang="ts">
import { computed } from 'vue'
import type { KnowledgeCardPayload } from '@/types/chat'

/**
 * 难点讲解知识卡的结构化展示。
 *
 * 定位：图（Seedream 出的信息图）是**表现形式**，这份结构才是内容。两者都渲染的理由：
 * 1. 图长得不对/文字糊了时，用户仍能读到正确内容；
 * 2. 出图是可选步骤（`degraded=true`），没图时这张卡就是全部交付物；
 * 3. 卡里的文字可选中、可复制，图片不行。
 *
 * 渲染纪律：载荷里有什么画什么，**不猜、不补**。缺字段就不渲染那一块，
 * 而不是显示"暂无数据"——知识卡是成品，空块只会显得没做完。
 */
const props = defineProps<{
  payload: KnowledgeCardPayload
  /** 这条消息里是否已经有配图（有图时不再提示"未生成配图"） */
  hasImage: boolean
}>()

const card = computed(() => props.payload?.card ?? null)
const points = computed(() => card.value?.points ?? [])

/** 没出图时的原因：优先用后端翻好的「可行动」说明，取不到才退回通用文案。
 *
 * 为什么要用后端的：出图是静默降级，笼统一句"未配置或调用失败"会把
 * 「没配 Key」和「方舟额度耗尽」混成一句——前者用户自己能修，后者只能找管理员。
 */
const imageHint = computed(
  () => props.payload?.degraded_reason?.trim() || '本次未生成配图（图像服务未配置或调用失败），上面的内容不受影响。',
)
</script>

<template>
  <div v-if="card" class="knowledge-card">
    <div class="knowledge-card__head">
      <h4 class="knowledge-card__title">{{ card.title }}</h4>
      <span v-if="card.topic" class="knowledge-card__topic">{{ card.topic }}</span>
    </div>

    <p v-if="card.summary" class="knowledge-card__summary">{{ card.summary }}</p>

    <ul v-if="points.length" class="knowledge-card__points">
      <li v-for="(point, idx) in points" :key="idx" class="knowledge-card__point">
        <span class="knowledge-card__point-title">{{ point.title }}</span>
        <span v-if="point.tip" class="knowledge-card__point-tip">{{ point.tip }}</span>
      </li>
    </ul>

    <div v-if="card.pitfall || card.correct" class="knowledge-card__contrast">
      <p v-if="card.pitfall" class="knowledge-card__pitfall">
        <span class="knowledge-card__label">常见误区</span>{{ card.pitfall }}
      </p>
      <p v-if="card.correct" class="knowledge-card__correct">
        <span class="knowledge-card__label">正确理解</span>{{ card.correct }}
      </p>
    </div>

    <p v-if="!hasImage" class="knowledge-card__hint">{{ imageHint }}</p>
  </div>
</template>

<style scoped lang="scss">
.knowledge-card {
  margin-top: 10px;
  padding: 12px 14px;
  border: 1px solid #d8dce4;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.6);
  font-size: 15px;
}

.knowledge-card__head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.knowledge-card__title {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
  color: #2f3642;
}

.knowledge-card__topic {
  color: #7b8290;
  font-size: 13px;
}

.knowledge-card__summary {
  margin: 8px 0 0;
  color: #3f4856;
  line-height: 1.65;
}

.knowledge-card__points {
  margin: 10px 0 0;
  padding-left: 18px;
}

.knowledge-card__point {
  margin-bottom: 4px;
  line-height: 1.6;
}

.knowledge-card__point-title {
  font-weight: 500;
  color: #2f3642;
}

.knowledge-card__point-tip {
  margin-left: 6px;
  color: #526173;
}

.knowledge-card__contrast {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #dfe4ec;
}

.knowledge-card__contrast p {
  margin: 0 0 6px;
  line-height: 1.6;
}

.knowledge-card__contrast p:last-child {
  margin-bottom: 0;
}

.knowledge-card__label {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 12px;
  background: #eef2f8;
  color: #526173;
}

.knowledge-card__pitfall {
  color: #bf5b48;
}

.knowledge-card__correct {
  color: #2f7a4f;
}

.knowledge-card__hint {
  margin: 10px 0 0;
  color: #8b93a4;
  font-size: 13px;
}
</style>
