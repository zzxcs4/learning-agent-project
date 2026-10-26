<script setup lang="ts">
import { ref } from 'vue'
import type { MessageSegment } from '@/types/chat'

const props = defineProps<{
  segment: MessageSegment
}>()

const imageLoadError = ref(false)
const audioLoadError = ref(false)
const fallbackImg = '/vite.svg'

// 附件名缺失时按类型给默认名，避免 UI 出现空文本。
const fileName = (segment: MessageSegment) => segment.name || `未命名${segment.type}文件`
</script>

<template>
  <div class="msg-file">
    <template v-if="props.segment.type === 'file'">
      <a
        class="file-link"
        :href="props.segment.url || '#'"
        target="_blank"
        rel="noopener noreferrer"
        :class="{ 'file-link--disabled': !props.segment.url }"
      >
        {{ fileName(props.segment) }}
      </a>
    </template>

    <template v-else-if="props.segment.type === 'image'">
      <a
        class="image-link"
        :href="props.segment.url || fallbackImg"
        target="_blank"
        rel="noopener noreferrer"
      >
        <img
          class="image"
          :src="imageLoadError ? fallbackImg : props.segment.url || fallbackImg"
          :alt="fileName(props.segment)"
          @error="imageLoadError = true"
        />
      </a>
      <!-- 图片容错：url 缺失或加载失败时都回退到占位图。 -->
      <p v-if="!props.segment.url" class="hint">图片链接缺失，已显示占位图</p>
      <p v-if="imageLoadError" class="hint">图片加载失败，已显示占位图</p>
    </template>

    <template v-else-if="props.segment.type === 'audio'">
      <!-- 音频容错：链接无效时给出可见提示，避免“无响应”体验。 -->
      <audio v-if="props.segment.url" class="audio" :src="props.segment.url" controls @error="audioLoadError = true"></audio>
      <p v-else class="hint">音频链接缺失，当前无法播放</p>
      <p v-if="audioLoadError" class="hint">音频加载失败，请检查资源链接</p>
    </template>
  </div>
</template>

<style scoped lang="scss">
.msg-file {
  margin-top: 8px;
}

.file-link {
  color: #3b6ea8;
  text-decoration: underline;
}

.file-link--disabled {
  color: #8b93a4;
  pointer-events: none;
  text-decoration: none;
}

.image {
  width: min(560px, 100%);
  max-height: 420px;
  object-fit: contain;
  border-radius: 10px;
  border: 1px solid #dfe5ef;
  display: block;
  background: #f5f8fd;
}

.image-link {
  display: inline-block;
}

.audio {
  width: min(320px, 100%);
}

.hint {
  margin: 6px 0 0;
  color: #bf5b48;
  font-size: 12px;
}
</style>
