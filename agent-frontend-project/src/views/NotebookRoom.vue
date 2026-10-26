<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  createNote,
  deleteNote,
  getNote,
  listNotes,
  updateNote,
  type NoteItem,
  type NoteListItem,
} from '@/api/notes'
import AppHeader from '@/components/layout/AppHeader.vue'
import SideBar from '@/components/layout/SideBar.vue'
import NotebookEditor from '@/components/notebook/NotebookEditor.vue'
import { tabs } from '@/composables/useRoomShell'

const PAGE_SIZE = 20
// 首屏最多翻这么多页，避免笔记很多时把首屏撑爆
const MAX_PAGES = 5

const route = useRoute()
const router = useRouter()

const activeTab = ref('notebook')
const notes = ref<NoteListItem[]>([])
const activeNoteId = ref<number | null>(null)
const detail = ref<NoteItem | null>(null)
const draftTitle = ref('')
const draftContent = ref('')
const listLoading = ref(false)
const detailLoading = ref(false)
const saving = ref(false)
const hasMore = ref(false)
const page = ref(1)

// 侧栏用的轻量结构（复用会话侧栏组件）
const sidebarItems = computed(() => notes.value.map((note) => ({ id: note.id, title: note.title })))
const dirty = computed(() => {
  if (!detail.value) return false
  return (
    draftTitle.value !== (detail.value.title ?? '') ||
    draftContent.value !== (detail.value.content ?? '')
  )
})

const updatedAtLabel = computed(() => {
  const ts = detail.value?.updated_at
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
})

const handleTabClick = async (tabKey: string) => {
  const selected = tabs.find((tab) => tab.key === tabKey)
  if (!selected) return
  activeTab.value = tabKey
  await router.push(selected.path)
}

const loadNotes = async (reset = false) => {
  if (listLoading.value) return
  listLoading.value = true
  try {
    if (reset) {
      notes.value = []
      page.value = 1
    }
    const data = await listNotes({ page: page.value, page_size: PAGE_SIZE })
    notes.value = reset ? data.notes : [...notes.value, ...data.notes]
    hasMore.value = data.has_more
    page.value += 1
    // 没有选中项时默认打开第一篇
    const first = notes.value[0]
    if (!activeNoteId.value && first) {
      await selectNote(first.id, true)
    }
  } finally {
    listLoading.value = false
  }
}

// 切换笔记前若还有未保存修改，先确认（force=true 用于首屏自动选中）
const selectNote = async (id: number, force = false) => {
  if (!force && id === activeNoteId.value) return
  if (!force && dirty.value) {
    try {
      await ElMessageBox.confirm('当前笔记有未保存的修改，切换后会丢失，继续吗？', '未保存的修改', {
        type: 'warning',
      })
    } catch {
      return
    }
  }
  detailLoading.value = true
  try {
    const note = await getNote(id)
    activeNoteId.value = id
    detail.value = note
    draftTitle.value = note.title
    draftContent.value = note.content
  } finally {
    detailLoading.value = false
  }
}

const create = async () => {
  try {
    const result = (await ElMessageBox.prompt('给这篇笔记起个标题', '新建笔记', {
      inputValue: '',
      inputPattern: /\S+/,
      inputErrorMessage: '标题不能为空',
    })) as { value: string }
    const title = result.value.trim()
    // 后端要求 content 非空，给一个可直接编辑的标题模板
    const note = await createNote({ title, content: `# ${title}\n\n` })
    notes.value = [
      { id: note.id, note_type: note.note_type, title: note.title, created_at: note.created_at, updated_at: note.updated_at },
      ...notes.value,
    ]
    activeNoteId.value = note.id
    detail.value = note
    draftTitle.value = note.title
    draftContent.value = note.content
    ElMessage.success('笔记已创建')
  } catch {
    // 用户取消不提示
  }
}

const save = async () => {
  if (!detail.value || !dirty.value || saving.value) return
  if (!draftTitle.value.trim()) {
    ElMessage.warning('标题不能为空')
    return
  }
  saving.value = true
  try {
    const updated = await updateNote(detail.value.id, {
      title: draftTitle.value.trim(),
      content: draftContent.value,
    })
    detail.value = updated
    // 同步侧栏标题，避免"改了标题但列表还是旧的"
    const index = notes.value.findIndex((note) => note.id === updated.id)
    if (index >= 0) {
      notes.value[index] = {
        id: updated.id,
        note_type: updated.note_type,
        title: updated.title,
        created_at: updated.created_at,
        updated_at: updated.updated_at,
      }
    }
    ElMessage.success('已保存')
  } finally {
    saving.value = false
  }
}

const remove = async (id?: number) => {
  const targetId = id ?? detail.value?.id
  if (!targetId) return
  const target = notes.value.find((note) => note.id === targetId)
  try {
    await ElMessageBox.confirm(`确认删除笔记「${target?.title ?? ''}」吗？`, '删除笔记', {
      type: 'warning',
    })
    await deleteNote(targetId)
    notes.value = notes.value.filter((note) => note.id !== targetId)
    if (activeNoteId.value === targetId) {
      const next = notes.value[0]
      activeNoteId.value = null
      detail.value = null
      draftTitle.value = ''
      draftContent.value = ''
      if (next) await selectNote(next.id, true)
    }
    ElMessage.success('笔记已删除')
  } catch {
    // 用户取消不提示
  }
}

// 侧栏的"改标题"：只改草稿并触发保存（避免直接调接口绕开编辑器状态）
const editTitle = (id: number | string) => {
  const targetId = Number(id)
  if (targetId !== activeNoteId.value) {
    void selectNote(targetId)
  }
  ElMessage.info('在右侧标题框里改标题，然后点「保存」')
}

onMounted(async () => {
  const tab = tabs.find((item) => item.path === route.path)
  if (tab) activeTab.value = tab.key
  await loadNotes(true)
  while (hasMore.value && page.value <= MAX_PAGES) {
    await loadNotes(false)
  }
})
</script>

<template>
  <div class="notebook-room">
    <AppHeader :tabs="tabs" :active-tab="activeTab" @tab-click="handleTabClick" />
    <div class="notebook-room__content">
      <SideBar
        :sessions="sidebarItems"
        :active-session-id="activeNoteId ?? ''"
        create-label="新建笔记"
        section-title="我的笔记"
        @create-session="create"
        @session-click="selectNote(Number($event))"
        @edit-session="editTitle"
        @remove-session="remove(Number($event))" />

      <div v-loading="detailLoading" class="notebook-room__main">
        <el-empty v-if="!detail" description="还没有笔记，点左侧「新建笔记」开始记录">
          <el-button type="primary" @click="create">新建笔记</el-button>
        </el-empty>
        <NotebookEditor
          v-else
          :title="draftTitle"
          :content="draftContent"
          :dirty="dirty"
          :saving="saving"
          :updated-at-label="updatedAtLabel"
          :source-run-id="detail.source_run_id"
          @update-title="draftTitle = $event"
          @update-content="draftContent = $event"
          @save="save"
          @remove="remove()" />
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.notebook-room {
  width: 100%;
  height: 100vh;
  display: flex;
  flex-direction: column;
}

.notebook-room__content {
  flex: 1;
  min-height: 0;
  display: flex;
}

.notebook-room__main {
  flex: 1;
  min-width: 0;
  display: flex;
}
</style>
