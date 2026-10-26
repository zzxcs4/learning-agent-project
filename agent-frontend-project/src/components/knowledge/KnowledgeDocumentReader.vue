<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft,
  Delete,
  Document,
  Download,
  EditPen,
  FolderOpened,
  Notebook,
  Refresh,
  Search,
  View,
} from '@element-plus/icons-vue'
import {
  deleteKnowledgeBaseResource,
  fetchResourceFile,
  getKnowledgeBase,
  getKnowledgeBaseResource,
  ingestStatusView,
  listKnowledgeBaseGroups,
  listKnowledgeBaseResources,
  updateKnowledgeBaseResource,
  type QuizCore,
  type QuizCoreSection,
  type ResourceDetail,
  type ResourceGroupItem,
  type ResourceItem,
} from '@/api/knowledgeBase'

const props = defineProps<{
  kbId: number
  kbName: string
  /** 该库的考纲（quiz_core）：有章节就能"从考纲跳到原文片段" */
  quizCore?: QuizCore | null
}>()
const emit = defineEmits<{ back: [] }>()

const resources = ref<ResourceItem[]>([])
const groups = ref<ResourceGroupItem[]>([])
const listLoading = ref(false)
const listError = ref('')

/** 考纲：优先用父组件传进来的，没传就自己拉一次库详情（详情接口才带 quiz_core） */
const quizCore = ref<QuizCore | null>(props.quizCore ?? null)

const detail = ref<ResourceDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')

/** 右侧面板当前看什么：正文 / 切片索引 / 考纲 */
const view = ref<'text' | 'chunks' | 'outline'>('text')
/** 从切片索引或考纲跳过来时高亮哪一片（几秒后自动消失） */
const activeChunk = ref<number | null>(null)
const chunkRefs = ref<Record<number, HTMLElement | null>>({})
let highlightTimer: ReturnType<typeof setTimeout> | null = null

const pdfPreviewUrl = ref('')
const previewLoading = ref(false)

// ---------------------------------------------------------------- 列表
const loadResources = async (selectId?: number) => {
  listLoading.value = true
  listError.value = ''
  try {
    const data = await listKnowledgeBaseResources(props.kbId, {
      keyword: keyword.value,
      groupName: activeGroup.value ?? undefined,
    })
    resources.value = data.items
    // 刷新后优先保持当前打开的那份；不在了才回落到第一份（搜索时回落会让人以为搜索失效）
    const wanted = (selectId ?? detail.value?.id) ?? undefined
    const keep = wanted !== undefined ? resources.value.find((row) => row.id === wanted) : undefined
    const target = keep ?? resources.value[0]
    if (target) {
      // 同一份就不用重新拉详情（例如只是改了个名字）
      if (detail.value?.id === target.id) {
        await openResource(target, true)
      } else {
        await openResource(target)
      }
    } else {
      detail.value = null
    }
    // 库内分组与当前关键词/分组无关，只在需要时刷新
    if (!searching.value) void loadGroups()
  } catch (err) {
    listError.value = err instanceof Error ? err.message : '文档列表加载失败'
  } finally {
    listLoading.value = false
  }
}

const loadGroups = async () => {
  try {
    const data = await listKnowledgeBaseGroups(props.kbId)
    groups.value = data.items
  } catch {
    // 分组拉不到不影响阅读，只是少了筛选条
    groups.value = []
  }
}

/**
 * 打开一份文档。参数用**最小契约** `{ id }`：列表项传 `ResourceItem`、详情里"重新读取"
 * 传 `ResourceDetail`，两者都可能传进来（`ResourceDetail` 没有 `has_text`，
 * 用它当参数类型会让详情对象传不进来——这正是之前 `vue-tsc -b` 报的错）。
 */
const openResource = async (item: { id: number }, force = false) => {
  if (!force && detail.value?.id === item.id) return
  releasePdf()
  detailLoading.value = true
  detailError.value = ''
  try {
    detail.value = await getKnowledgeBaseResource(props.kbId, item.id)
    if (!force) {
      view.value = 'text'
      activeChunk.value = null
    }
  } catch (err) {
    detail.value = null
    detailError.value = err instanceof Error ? err.message : '文档读取失败'
  } finally {
    detailLoading.value = false
  }
}

// ---------------------------------------------------------------- 搜索 / 分组（功能 2、3）
const keyword = ref('')
/** 当前分组过滤：undefined=全部，''=未分组，其它=具体分组名 */
const activeGroup = ref<string | undefined>(undefined)
/** 是否处于搜索中：决定是否显示命中片段与自动打开第一份的行为 */
const searching = computed(() => keyword.value.trim().length > 0)
let searchTimer: ReturnType<typeof setTimeout> | null = null

const onKeywordInput = () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    void loadResources()
  }, 350)
}

const clearKeyword = () => {
  keyword.value = ''
  void loadResources()
}

const pickGroup = (name: string | undefined) => {
  activeGroup.value = name
  void loadResources()
}

/** 分组筛选条的文案：未分组单独一档 */
const groupLabel = (name: string | null): string => name ?? '未分组'

/** 列表按分组分段展示（搜索时不分段，直接平铺命中结果） */
const groupedResources = computed(() => {
  if (searching.value) {
    return [{ name: null as string | null, label: '', items: resources.value }]
  }
  const buckets = new Map<string, ResourceItem[]>()
  for (const item of resources.value) {
    const key = item.group_name ?? ''
    const list = buckets.get(key)
    if (list) list.push(item)
    else buckets.set(key, [item])
  }
  const named = [...buckets.entries()]
    .filter(([key]) => key !== '')
    .sort((a, b) => a[0].localeCompare(b[0], 'zh-Hans-CN'))
    .map(([key, items]) => ({ name: key as string | null, label: key, items }))
  const ungrouped = buckets.get('') ?? []
  return ungrouped.length
    ? [...named, { name: null as string | null, label: '未分组', items: ungrouped }]
    : named
})

// ---------------------------------------------------------------- 管理：改名 / 移动 / 删除（功能 1、2）
const renameVisible = ref(false)
const renameValue = ref('')
const submitting = ref(false)

/**
 * 删除前取一次详情：搜索状态下列表项的 `chunk_count` 是**命中片数**而不是总片数，
 * 拿它写进"会同时删除 N 个切片"的确认文案会少报（实测搜"索引"时 2 片的资料报成 1 片）。
 * 详情里的 `chunk_count` 才是权威总数。
 */
/**
 * 只取"权威片数"就够，所以参数用**最小契约**而不是 `ResourceItem`：
 * 详情接口的返回类型是 `ResourceDetail`（没有 `has_text`），列表项才是 `ResourceItem`。
 * 用 `ResourceItem` 当参数会导致详情对象传不进来——这正是 `vue-tsc -b` 之前报的错。
 */
const deleteTargetChunks = async (item: { id: number; chunk_count: number }): Promise<number> => {
  try {
    const fresh = await getKnowledgeBaseResource(props.kbId, item.id)
    return fresh.chunk_count
  } catch {
    // 详情取不到就别拿命中片数糊弄用户，改为不承诺具体片数
    return searching.value ? 0 : item.chunk_count
  }
}

const openRename = () => {
  if (!detail.value) return
  renameValue.value = detail.value.file_name
  renameVisible.value = true
}

const submitRename = async () => {
  if (!detail.value) return
  const name = renameValue.value.trim()
  if (!name) {
    ElMessage.warning('文件名不能为空')
    return
  }
  submitting.value = true
  try {
    const updated = await updateKnowledgeBaseResource(props.kbId, detail.value.id, {
      fileName: name,
    })
    detail.value = updated
    renameVisible.value = false
    ElMessage.success('已重命名')
    await loadResources(updated.id)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '重命名失败')
  } finally {
    submitting.value = false
  }
}

const moveVisible = ref(false)
const moveTarget = ref<string | undefined>(undefined)
const moveNewGroup = ref('')

const openMove = () => {
  if (!detail.value) return
  moveTarget.value = detail.value.group_name ?? undefined
  moveNewGroup.value = ''
  moveVisible.value = true
}

const submitMove = async () => {
  if (!detail.value) return
  // 填了"新建分组"就用它；否则用选中的既有分组（null 表示移出分组）
  const target = moveNewGroup.value.trim() || moveTarget.value || ''
  submitting.value = true
  try {
    const updated = await updateKnowledgeBaseResource(props.kbId, detail.value.id, {
      groupName: target,
    })
    detail.value = updated
    moveVisible.value = false
    ElMessage.success(target ? `已移动到「${target}」` : '已移出分组')
    await loadResources(updated.id)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '移动失败')
  } finally {
    submitting.value = false
  }
}

const confirmDelete = async () => {
  if (!detail.value) return
  const target = detail.value
  const chunkText = await deleteTargetChunks(target)
  const scope = chunkText > 0 ? `${chunkText} 个切片、对应的向量索引与原文件` : '它的切片、向量索引与原文件'
  try {
    // ⚠️ `ElMessageBox.confirm` 解析出来的是**动作字符串** `'confirm'`，不是 `{ value }`
    // ——`{ value }` 只有 `prompt`（showInput）才会返回。这里曾经写成
    // `const confirmed = await ...; if (!confirmed?.value) return`，取值恒为 undefined，
    // 于是"删除文档"点了确认**静默什么都不做**（不报错、不删除、没有任何提示），
    // 看起来就像这个功能没做。取消/关闭会 reject，由下面的 catch 兜住，不需要再判返回值。
    await ElMessageBox.confirm(
      // 纯文本提示：ElMessageBox 不渲染 markdown/HTML，写 ** 只会原样显示成星号
      `确认删除「${target.file_name}」吗？会同时删除${scope}，不可撤销（出题与答疑都不再引用它）。`,
      '删除文档',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        // 主按钮用红色，避免"顺手点了确认"
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  submitting.value = true
  try {
    const result = await deleteKnowledgeBaseResource(props.kbId, target.id, true)
    ElMessage.success(
      `已删除：${result.deleted_chunk_count} 个切片、${result.deleted_vector_count} 个向量` +
        (result.deleted_file ? '，原文件也已清除' : '（原文件保留）') +
        // 考纲章节以这份资料为原文锚点，资料没了就再也定位不到原文，所以后端会一并摘掉
        (result.removed_quiz_sections > 0
          ? `，并从考纲里摘掉了 ${result.removed_quiz_sections} 个章节`
          : ''),
    )
    detail.value = null
    await loadResources()
    // 后端删资料时会把"以它为原文锚点"的考纲章节一并摘掉，所以要强制重取，
    // 否则考纲页还挂着刚被摘掉的那些章节（loadQuizCore 默认有"已有内容就不重取"的短路）
    await loadQuizCore(true)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  } finally {
    submitting.value = false
  }
}

// ---------------------------------------------------------------- 正文
/** 按空行切段渲染，保留段落感（正文是纯文本，不需要 markdown 转义） */
const paragraphs = computed(() => {
  const text = detail.value?.text ?? ''
  if (!text) return []
  return text.split(/\n{2,}/).map((block) => block.trim()).filter(Boolean)
})

const textSourceHint = computed(() => {
  switch (detail.value?.text_source) {
    case 'original':
      return '入库时保存的解析全文'
    case 'chunks':
      return '由切片拼回（老资料没有存全文，排版可能有损失；重新上传一次即可拿到更完整的正文）'
    default:
      return '这份资料还没入库成功，暂时读不到正文'
  }
})

// ---------------------------------------------------------------- 切片 / 考纲跳转
const outlineSections = computed<QuizCoreSection[]>(() => {
  const sections = quizCore.value?.sections
  return Array.isArray(sections) ? sections : []
})

/** 拿考纲：父组件没传（列表接口不带 quiz_core）就拉一次库详情 */
const loadQuizCore = async (force = false) => {
  if (!force && quizCore.value?.sections?.length) return
  try {
    const detailItem = await getKnowledgeBase(props.kbId)
    quizCore.value = detailItem.quiz_core ?? null
  } catch {
    // 拿不到考纲不影响读正文，考纲页显示空态即可
  }
}

const scrollToChunk = async (chunkIndex: number) => {
  activeChunk.value = chunkIndex
  view.value = 'chunks'
  await nextTick()
  const el = chunkRefs.value[chunkIndex]
  el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  if (highlightTimer) clearTimeout(highlightTimer)
  highlightTimer = setTimeout(() => {
    activeChunk.value = null
  }, 4000)
}

/** 考纲章节 → 该章正文对应的切片（后端 `source_ref` 存的是 chunk_from/to） */
const jumpToSection = (section: QuizCoreSection) => {
  const ref = section.source_ref
  if (section.source_status === 'missing') {
    // 后端在删资料时已经会摘掉这类章节，所以这里只是兜底（例如历史数据）：
    // 别承诺"重新上传就能恢复"——章节锚的是被删的那个 resource_id，重传拿到的是新 id
    ElMessage.warning('这一章的原文资料已被删除，无法跳转；可以重新提炼本库考纲')
    return
  }
  if (!detail.value || !ref || ref.resource_id !== detail.value.id || ref.chunk_from === undefined) {
    ElMessage.info('这一章没有可定位的原文片段')
    return
  }
  void scrollToChunk(ref.chunk_from)
}

const sectionRange = (section: QuizCoreSection): string => {
  const ref = section.source_ref
  if (!ref || ref.chunk_from === undefined) return '无定位'
  const to = ref.chunk_to ?? ref.chunk_from
  return ref.chunk_from === to ? `第 ${ref.chunk_from} 片` : `第 ${ref.chunk_from}–${to} 片`
}

// ---------------------------------------------------------------- 原文件
const previewPdf = async () => {
  if (!detail.value) return
  previewLoading.value = true
  // 先开一个空白页占位：blob 是异步取回来的，等回来再开新标签会被浏览器当弹窗拦掉
  const tab = window.open('', '_blank')
  try {
    const { blob } = await fetchResourceFile(props.kbId, detail.value.id)
    releasePdf()
    pdfPreviewUrl.value = URL.createObjectURL(blob)
    if (tab) {
      tab.location.href = pdfPreviewUrl.value
    } else {
      ElMessage.info('浏览器拦截了新标签页，已改为在本页内嵌预览')
    }
  } catch (err) {
    tab?.close()
    ElMessage.error(err instanceof Error ? err.message : '原文件读取失败')
  } finally {
    previewLoading.value = false
  }
}

const downloadFile = async () => {
  if (!detail.value) return
  try {
    const { blob, filename } = await fetchResourceFile(props.kbId, detail.value.id, true)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '下载失败')
  }
}

const releasePdf = () => {
  if (pdfPreviewUrl.value) {
    URL.revokeObjectURL(pdfPreviewUrl.value)
    pdfPreviewUrl.value = ''
  }
}

// ---------------------------------------------------------------- 工具
const formatTime = (ts: number | null): string => {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

const formatChars = (n: number): string => (n >= 10000 ? `${(n / 10000).toFixed(1)} 万字` : `${n} 字`)

watch(
  () => [props.kbId, props.kbName],
  () => {
    releasePdf()
    detail.value = null
    resources.value = []
    groups.value = []
    keyword.value = ''
    activeGroup.value = undefined
    quizCore.value = props.quizCore ?? null
    void loadResources()
    void loadQuizCore()
  },
  { immediate: true },
)

// 组件卸载：清掉 object URL 与定时器
onBeforeUnmount(() => {
  releasePdf()
  if (highlightTimer) clearTimeout(highlightTimer)
  if (searchTimer) clearTimeout(searchTimer)
})
</script>

<template>
  <div class="kb-doc">
    <!-- 左栏：库内文档 -->
    <aside class="kb-doc__aside">
      <div class="kb-doc__aside-head">
        <el-button link :icon="ArrowLeft" @click="emit('back')">返回知识库</el-button>
        <div class="kb-doc__kb-name" :title="kbName">{{ kbName }}</div>
        <div class="kb-doc__aside-sub">
          {{ resources.length }} 份文档
          <span v-if="searching">（搜索「{{ keyword.trim() }}」的结果）</span>
        </div>
      </div>

      <!-- 库内搜索：文件名或正文命中都算 -->
      <div class="kb-doc__search">
        <el-input
          v-model="keyword"
          size="small"
          clearable
          placeholder="搜索文件名或正文"
          :prefix-icon="Search"
          @input="onKeywordInput"
          @clear="clearKeyword" />
      </div>

      <!-- 分组筛选（分组是 group_name 字符串，未分组单独一档） -->
      <div v-if="groups.length > 1" class="kb-doc__groups">
        <button
          type="button"
          class="kb-doc__group"
          :class="{ 'is-active': activeGroup === undefined }"
          @click="pickGroup(undefined)">
          全部 {{ resources.length }}
        </button>
        <button
          v-for="group in groups"
          :key="group.name ?? '__ungrouped__'"
          type="button"
          class="kb-doc__group"
          :class="{ 'is-active': activeGroup === (group.name ?? '') }"
          @click="pickGroup(group.name ?? '')">
          {{ groupLabel(group.name) }} {{ group.resource_count }}
        </button>
      </div>

      <div v-loading="listLoading" class="kb-doc__list">
        <el-empty v-if="listError" :description="listError" :image-size="60" />
        <el-empty
          v-else-if="!resources.length && !listLoading"
          :description="searching ? '没有匹配的文档' : '这个库还没有文档'"
          :image-size="60" />

        <template v-for="bucket in groupedResources" :key="bucket.label || '__flat__'">
          <div v-if="bucket.label" class="kb-doc__group-title">
            <el-icon><FolderOpened /></el-icon>
            {{ bucket.label }}
            <span class="kb-doc__group-count">{{ bucket.items.length }}</span>
          </div>
          <button
            v-for="item in bucket.items"
            :key="item.id"
            type="button"
            class="kb-doc__item"
            :class="{ 'is-active': detail?.id === item.id }"
            @click="openResource(item)">
            <el-icon class="kb-doc__item-icon"><Document /></el-icon>
            <span class="kb-doc__item-main">
              <span class="kb-doc__item-name" :title="item.file_name">{{ item.file_name }}</span>
              <span class="kb-doc__item-meta">
                <template v-if="searching">
                  命中 {{ item.matched_chunk_count }} 片 ·
                  {{ item.match_field === 'name' ? '文件名命中' : item.match_field === 'both' ? '文件名+正文命中' : '正文命中' }}
                </template>
                <template v-else>
                  {{ formatChars(item.char_count) }} · {{ item.chunk_count }} 片
                </template>
              </span>
              <!-- 搜索命中时给出片段，省一次点开 -->
              <span
                v-for="(snippet, index) in item.match_snippets"
                :key="index"
                class="kb-doc__snippet">
                {{ snippet }}
              </span>
            </span>
            <el-tag :type="ingestStatusView(item.ingest_status).type" size="small" effect="light">
              {{ ingestStatusView(item.ingest_status).label }}
            </el-tag>
          </button>
        </template>
      </div>
    </aside>

    <!-- 右栏：正文 / 切片 / 考纲 -->
    <section v-loading="detailLoading" class="kb-doc__main">
      <el-empty
        v-if="!detail && !detailLoading"
        :description="detailError || '从左边选一份文档开始读'"
        :image-size="80" />

      <template v-else-if="detail">
        <header class="kb-doc__head">
          <div class="kb-doc__title" :title="detail.file_name">{{ detail.file_name }}</div>
          <div class="kb-doc__meta">
            <el-tag :type="ingestStatusView(detail.ingest_status).type" size="small" effect="light">
              {{ ingestStatusView(detail.ingest_status).label }}
            </el-tag>
            <span v-if="detail.group_name">分组：{{ detail.group_name }}</span>
            <span>上传于 {{ formatTime(detail.create_time) }}</span>
            <span>{{ formatChars(detail.text_chars) }}</span>
            <span>{{ detail.chunk_count }} 个片段</span>
          </div>
          <div class="kb-doc__actions">
            <el-button
              v-if="detail.can_preview"
              size="small"
              :loading="previewLoading"
              :icon="View"
              @click="previewPdf">
              预览原文件
            </el-button>
            <el-button
              v-if="detail.can_preview"
              size="small"
              plain
              :icon="Download"
              @click="downloadFile">
              下载
            </el-button>
            <el-button size="small" plain :icon="EditPen" @click="openRename">重命名</el-button>
            <el-button size="small" plain :icon="FolderOpened" @click="openMove">移动分组</el-button>
            <el-button size="small" plain :icon="Refresh" @click="openResource(detail, true)">
              重新读取
            </el-button>
            <el-button size="small" type="danger" plain :icon="Delete" @click="confirmDelete">
              删除
            </el-button>
          </div>
        </header>

        <!-- 原文件内联预览（PDF） -->
        <div v-if="pdfPreviewUrl" class="kb-doc__pdf">
          <div class="kb-doc__pdf-bar">
            <span>原文件预览（PDF）</span>
            <el-button link type="primary" @click="releasePdf">收起</el-button>
          </div>
          <iframe :src="pdfPreviewUrl" class="kb-doc__pdf-frame" title="原文件预览" />
        </div>

        <el-alert
          v-if="detail.text_source === 'chunks'"
          class="kb-doc__notice"
          type="warning"
          :closable="false"
          show-icon
          title="正文由切片拼回"
          :description="textSourceHint" />

        <el-tabs v-model="view" class="kb-doc__tabs">
          <el-tab-pane name="text">
            <template #label>
              <span class="kb-doc__tab">正文</span>
            </template>
            <div class="kb-doc__text">
              <p v-for="(para, index) in paragraphs" :key="index">{{ para }}</p>
              <el-empty
                v-if="!paragraphs.length"
                description="这份资料暂时读不到正文（可能还在入库，或原始文件是扫描件）"
                :image-size="70" />
            </div>
          </el-tab-pane>

          <el-tab-pane name="chunks">
            <template #label>
              <span class="kb-doc__tab">切片 {{ detail.chunks.length }}</span>
            </template>
            <div class="kb-doc__chunks">
              <article
                v-for="chunk in detail.chunks"
                :key="chunk.chunk_index"
                :ref="(el) => { chunkRefs[chunk.chunk_index] = (el as HTMLElement | null) }"
                class="kb-doc__chunk"
                :class="{ 'is-active': activeChunk === chunk.chunk_index }">
                <div class="kb-doc__chunk-head">
                  <span class="kb-doc__chunk-no">#{{ chunk.chunk_index }}</span>
                  <span class="kb-doc__chunk-len">{{ chunk.char_count }} 字</span>
                </div>
                <p class="kb-doc__chunk-text">{{ chunk.text }}</p>
              </article>
              <el-empty
                v-if="!detail.chunks.length"
                description="还没有切片（入库未完成）"
                :image-size="70" />
            </div>
          </el-tab-pane>

          <el-tab-pane name="outline">
            <template #label>
              <span class="kb-doc__tab">
                <el-icon><Notebook /></el-icon>
                考纲 {{ outlineSections.length }}
              </span>
            </template>
            <div class="kb-doc__outline">
              <p class="kb-doc__outline-tip">
                考纲是这份资料被提炼出的"考什么"；点「跳到原文」会切到切片视图并定位到对应片段。
              </p>
              <el-empty
                v-if="!outlineSections.length"
                description="这个库还没有考纲章节（上传资料后会自动提炼）"
                :image-size="70" />
              <el-card
                v-for="(section, index) in outlineSections"
                :key="index"
                class="kb-doc__section"
                shadow="never">
                <div class="kb-doc__section-head">
                  <span class="kb-doc__section-name">{{ section.heading || '未命名章节' }}</span>
                  <span class="kb-doc__section-ref">{{ sectionRange(section) }}</span>
                </div>
                <div class="kb-doc__topics">
                  <el-tag
                    v-for="topic in section.topics || []"
                    :key="topic"
                    size="small"
                    effect="plain"
                    class="kb-doc__topic">
                    {{ topic }}
                  </el-tag>
                </div>
                <div class="kb-doc__section-foot">
                  <span>被选作出题上下文 {{ section.asked_count || 0 }} 次</span>
                  <el-tag
                    v-if="section.source_status === 'missing'"
                    size="small"
                    type="info"
                    effect="plain">
                    原文资料已删除
                  </el-tag>
                  <el-button
                    link
                    type="primary"
                    :disabled="section.source_status === 'missing' || !detail || section.source_ref?.resource_id !== detail.id"
                    @click="jumpToSection(section)">
                    跳到原文
                  </el-button>
                </div>
              </el-card>
            </div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </section>

    <!-- 重命名 -->
    <el-dialog v-model="renameVisible" title="重命名文档" width="440px" append-to-body>
      <el-input v-model="renameValue" maxlength="255" show-word-limit @keyup.enter="submitRename" />
      <p class="kb-doc__dialog-tip">
        只改文档名，正文与切片不变。改完后出题页面看到的就是新名字。
      </p>
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitRename">保存</el-button>
      </template>
    </el-dialog>

    <!-- 移动分组 -->
    <el-dialog v-model="moveVisible" title="移动分组" width="440px" append-to-body>
      <p class="kb-doc__dialog-tip">分组只是库内的归档方式（纯名字），不影响检索与出题。</p>
      <el-radio-group v-model="moveTarget" class="kb-doc__move-list">
        <el-radio :value="undefined">不分组</el-radio>
        <el-radio v-for="group in groups.filter((row) => row.name)" :key="group.name" :value="group.name">
          {{ group.name }}
        </el-radio>
      </el-radio-group>
      <el-input
        v-model="moveNewGroup"
        class="kb-doc__move-new"
        maxlength="64"
        placeholder="或输入新分组名（优先使用）" />
      <template #footer>
        <el-button @click="moveVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitMove">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.kb-doc {
  display: flex;
  gap: 16px;
  min-height: 520px;
}

.kb-doc__aside {
  width: 300px;
  flex-shrink: 0;
  border: 1px solid #e4e7ed;
  border-radius: 10px;
  background: #ffffff;
  display: flex;
  flex-direction: column;
  max-height: 74vh;
}

.kb-doc__aside-head {
  padding: 12px 14px 10px;
  border-bottom: 1px solid #eef0f3;
}

.kb-doc__kb-name {
  margin-top: 6px;
  font-size: 15px;
  font-weight: 600;
  color: #30353e;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-doc__aside-sub {
  margin-top: 2px;
  font-size: 12px;
  color: #909399;
}

.kb-doc__list {
  flex: 1;
  min-height: 120px;
  overflow-y: auto;
  padding: 8px;
}

.kb-doc__search {
  padding: 0 10px 8px;
  border-bottom: 1px solid #f2f4f7;
}

.kb-doc__groups {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 10px;
  border-bottom: 1px solid #f2f4f7;
}

.kb-doc__group {
  padding: 2px 8px;
  border: 1px solid #e4e7ed;
  border-radius: 10px;
  background: #ffffff;
  font-size: 12px;
  color: #596272;
  cursor: pointer;

  &:hover {
    border-color: #1f1f1f;
  }

  &.is-active {
    background: #f2f3f5;
    border-color: #1f1f1f;
    color: #303133;
  }
}

.kb-doc__group-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 8px 4px 6px;
  font-size: 12px;
  color: #8a9099;
}

.kb-doc__group-count {
  color: #b4b8bf;
}

.kb-doc__snippet {
  margin-top: 4px;
  padding: 4px 6px;
  border-radius: 4px;
  background: #f2f3f5;
  font-size: 12px;
  line-height: 1.6;
  color: #596272;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.kb-doc__dialog-tip {
  margin: 10px 0 0;
  font-size: 12px;
  color: #909399;
  line-height: 1.7;
}

.kb-doc__move-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
}

.kb-doc__move-new {
  margin-top: 10px;
}

.kb-doc__item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  margin-bottom: 6px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: #f7f8fa;
  cursor: pointer;
  text-align: left;

  &:hover {
    background: #eef2f8;
  }

  &.is-active {
    background: #f2f3f5;
    border-color: #1f1f1f;
  }
}

.kb-doc__item-icon {
  color: #8a9099;
  flex-shrink: 0;
}

.kb-doc__item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.kb-doc__item-name {
  font-size: 13px;
  color: #30353e;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-doc__item-meta {
  font-size: 12px;
  color: #909399;
}

.kb-doc__main {
  flex: 1;
  min-width: 0;
  border: 1px solid #e4e7ed;
  border-radius: 10px;
  background: #ffffff;
  padding: 16px 18px;
  max-height: 74vh;
  overflow-y: auto;
}

.kb-doc__head {
  margin-bottom: 10px;
}

.kb-doc__title {
  font-size: 17px;
  font-weight: 600;
  color: #30353e;
  word-break: break-all;
}

.kb-doc__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
}

.kb-doc__actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.kb-doc__notice {
  margin-bottom: 8px;
}

.kb-doc__pdf {
  margin-bottom: 12px;
}

.kb-doc__pdf-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  color: #606266;
  margin-bottom: 6px;
}

.kb-doc__pdf-frame {
  width: 100%;
  height: 62vh;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
}

.kb-doc__tab {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.kb-doc__text {
  font-size: 15px;
  line-height: 2;
  color: #2f3339;
  max-width: 900px;

  p {
    margin: 0 0 14px;
    white-space: pre-wrap;
    word-break: break-word;
  }
}

.kb-doc__chunks {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.kb-doc__chunk {
  border: 1px solid #eef0f3;
  border-left: 3px solid #dfe3ea;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fbfcfd;
  transition: border-color 0.2s, background 0.2s;

  &.is-active {
    border-left-color: #1f1f1f;
    background: #f2f3f5;
  }
}

.kb-doc__chunk-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
  font-size: 12px;
  color: #909399;
}

.kb-doc__chunk-no {
  font-weight: 600;
  color: #596272;
}

.kb-doc__chunk-text {
  margin: 0;
  font-size: 14px;
  line-height: 1.9;
  color: #2f3339;
  white-space: pre-wrap;
  word-break: break-word;
}

.kb-doc__outline-tip {
  margin: 0 0 12px;
  font-size: 13px;
  color: #909399;
}

.kb-doc__section {
  margin-bottom: 10px;
  border: 1px solid #eef0f3;
}

.kb-doc__section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.kb-doc__section-name {
  font-size: 15px;
  font-weight: 600;
  color: #30353e;
}

.kb-doc__section-ref {
  font-size: 12px;
  color: #909399;
  white-space: nowrap;
}

.kb-doc__topics {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0;
}

.kb-doc__topic {
  font-size: 12px;
}

.kb-doc__section-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: #909399;
}

@media (max-width: 1100px) {
  .kb-doc {
    flex-direction: column;
  }

  .kb-doc__aside {
    width: 100%;
    max-height: 240px;
  }
}
</style>
