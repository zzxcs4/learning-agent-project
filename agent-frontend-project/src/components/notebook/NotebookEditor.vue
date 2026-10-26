<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdownCached } from '@/utils/markdownUtil'

/**
 * 笔记编辑器：Markdown 源码 + 实时预览。
 *
 * 为什么不用富文本编辑器：后端 `notes.content` 约定是 **Markdown**（AI 归档写的也是 Markdown），
 * 富文本产出 HTML，两者混存会让同一篇笔记一半是 HTML 一半是 Markdown。
 * 这里用 Markdown 源码编辑，天然与 AI 归档的笔记一致，也不需要额外的 HTML→Markdown 依赖。
 */
const props = defineProps<{
  title: string
  content: string
  dirty: boolean
  saving: boolean
  updatedAtLabel: string
  sourceRunId?: string | null
  disabled?: boolean
}>()

const emit = defineEmits<{
  updateTitle: [value: string]
  updateContent: [value: string]
  save: []
  remove: []
}>()

const titleModel = computed({
  get: () => props.title,
  set: (value: string) => emit('updateTitle', value),
})

const contentModel = computed({
  get: () => props.content,
  set: (value: string) => emit('updateContent', value),
})

// 预览按内容做缓存，输入时不重复解析整篇
const previewHtml = computed(() => renderMarkdownCached(props.content || '', 'note-preview'))
</script>

<template>
  <section class="note-editor">
    <div class="note-editor__card">
      <div class="note-editor__header">
        <el-input
          v-model="titleModel"
          class="note-editor__title"
          placeholder="笔记标题"
          :disabled="disabled" />
        <div class="note-editor__actions">
          <el-tag v-if="sourceRunId" size="small" type="info" effect="plain">AI 归档</el-tag>
          <span v-if="dirty" class="note-editor__dirty">未保存</span>
          <span v-else class="note-editor__saved">已保存 · {{ updatedAtLabel }}</span>
          <el-button size="small" type="danger" plain :disabled="disabled" @click="emit('remove')">
            删除
          </el-button>
          <el-button
            size="small"
            type="primary"
            :loading="saving"
            :disabled="disabled || !dirty"
            @click="emit('save')">
            保存
          </el-button>
        </div>
      </div>

      <div class="note-editor__body">
        <div class="note-editor__pane">
          <div class="note-editor__pane-label">Markdown</div>
          <el-input
            v-model="contentModel"
            class="note-editor__source"
            type="textarea"
            resize="none"
            placeholder="用 Markdown 记录：标题、要点、代码块…"
            :disabled="disabled" />
        </div>
        <div class="note-editor__pane">
          <div class="note-editor__pane-label">预览</div>
          <div class="note-editor__preview markdown-body" v-html="previewHtml" />
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped lang="scss">
.note-editor {
  flex: 1;
  min-width: 0;
  /* 透明：外层不再铺米色，背景由全局 DynamicBackground 提供；
     内层 .note-editor__card 仍是不透明的编辑面，保证书写区域干净可读。 */
  background: transparent;
  padding: 24px 26px;
  overflow: hidden;
}

.note-editor__card {
  width: min(1180px, 100%);
  height: 100%;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  background: #f5f5f6;
  border: 1px solid #e1e3e8;
  border-radius: 12px;
  padding: 18px 20px 20px;
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.08);
}

.note-editor__header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid #e1e3e8;
}

.note-editor__title {
  flex: 1;
  min-width: 0;

  :deep(.el-input__wrapper) {
    box-shadow: none;
    background: transparent;
  }

  :deep(.el-input__inner) {
    height: 44px;
    font-size: 24px;
    font-weight: 600;
    color: #30353e;
  }
}

.note-editor__actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.note-editor__dirty {
  font-size: 12px;
  color: #d9480f;
}

.note-editor__saved {
  font-size: 12px;
  color: #8a9099;
}

.note-editor__body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  padding-top: 16px;
}

.note-editor__pane {
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.note-editor__pane-label {
  margin-bottom: 6px;
  font-size: 12px;
  color: #8a9099;
}

.note-editor__source {
  flex: 1;
  min-height: 0;

  :deep(.el-textarea__inner) {
    height: 100%;
    min-height: 420px;
    border-radius: 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 14px;
    line-height: 1.8;
    background: #ffffff;
  }
}

.note-editor__preview {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 18px;
  border: 1px solid #d8dbe2;
  border-radius: 10px;
  background: #ffffff;
  font-size: 15px;
  line-height: 1.85;
  color: #30353e;

  :deep(h1),
  :deep(h2),
  :deep(h3) {
    margin: 14px 0 8px;
  }

  :deep(table) {
    width: 100%;
    border-collapse: collapse;
  }

  :deep(th),
  :deep(td) {
    border: 1px solid #e1e3e8;
    padding: 6px 10px;
    text-align: left;
  }

  :deep(pre) {
    overflow-x: auto;
    border-radius: 8px;
  }
}

@media (max-width: 1100px) {
  .note-editor__body {
    grid-template-columns: 1fr;
  }
}
</style>
