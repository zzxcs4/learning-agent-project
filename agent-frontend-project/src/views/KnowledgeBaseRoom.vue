<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  deleteUnboundResource,
  isBuilding,
  listKnowledgeBases,
  listUnboundResources,
  quizStatusView,
  reassignUnboundResource,
  type KnowledgeBaseItem,
  type UnboundResourceItem,
} from '@/api/knowledgeBase'
import { uploadDocument } from '@/api/upload'
import AppHeader from '@/components/layout/AppHeader.vue'
import KnowledgeDocumentReader from '@/components/knowledge/KnowledgeDocumentReader.vue'
import { tabs } from '@/composables/useRoomShell'

// 轮询间隔：构建是后台异步的（向量入库 + 考纲提炼），列表据此刷新状态
const POLL_INTERVAL = 3000

const route = useRoute()
const router = useRouter()

const activeTab = ref('knowledge-base')
const items = ref<KnowledgeBaseItem[]>([])
const loading = ref(false)
const creating = ref(false)

// 打开某个库 = 进它的文档阅读器（null 表示还在知识库列表）
const openItem = ref<KnowledgeBaseItem | null>(null)

// 上传弹窗
const uploadVisible = ref(false)
const uploadTarget = ref<KnowledgeBaseItem | null>(null)
const uploadFile = ref<File | null>(null)
const uploading = ref(false)

// 未归档资料（删库时被解绑的那些）：切片与向量都还在、答疑仍检索得到，
// 但库内列表必须按库查，所以它们此前在界面上没有任何入口。这里给它们一个可见的位置。
const unboundItems = ref<UnboundResourceItem[]>([])
const unboundExpanded = ref(false)
const unboundLoading = ref(false)
const reassignTarget = ref<UnboundResourceItem | null>(null)
const reassignKbId = ref<number | undefined>(undefined)
const reassigning = ref(false)
const reassignVisible = ref(false)

const loadUnbound = async () => {
  unboundLoading.value = true
  try {
    const data = await listUnboundResources()
    unboundItems.value = data.items
  } catch {
    // 拿不到未归档列表不影响主流程（可能是老后端），静默即可
    unboundItems.value = []
  } finally {
    unboundLoading.value = false
  }
}

let pollTimer: ReturnType<typeof setTimeout> | null = null

// 是否还有库在构建中——决定要不要继续轮询（进阅读器后不再轮询列表）
const building = computed(() => !openItem.value && items.value.some((item) => isBuilding(item)))

const handleTabClick = async (tabKey: string) => {
  const selected = tabs.find((tab) => tab.key === tabKey)
  if (!selected) return
  activeTab.value = tabKey
  await router.push(selected.path)
}

const formatTime = (ts: number | null): string => {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

const load = async () => {
  if (loading.value) return
  loading.value = true
  try {
    const data = await listKnowledgeBases()
    items.value = data.items
    await loadUnbound()
  } finally {
    loading.value = false
    schedulePoll()
  }
}

// 只在"还有库在构建"时继续轮询，构建完自动停
const schedulePoll = () => {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
  if (!building.value) return
  pollTimer = setTimeout(() => {
    void load()
  }, POLL_INTERVAL)
}

const create = async () => {
  try {
    const result = (await ElMessageBox.prompt('给知识库起个名字（例如：MySQL 面试）', '新建知识库', {
      inputValue: '',
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
    })) as { value: string }
    creating.value = true
    const created = await createKnowledgeBase({ name: result.value.trim() })
    items.value = [created, ...items.value]
    ElMessage.success('知识库已创建，上传资料后会自动提炼考纲')
  } catch {
    // 用户取消不提示
  } finally {
    creating.value = false
  }
}

const openUpload = (item: KnowledgeBaseItem) => {
  uploadTarget.value = item
  uploadFile.value = null
  uploadVisible.value = true
}

// 进入某个知识库的文档阅读器
const enterKnowledgeBase = (item: KnowledgeBaseItem) => {
  openItem.value = item
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

// el-upload 选择文件（auto-upload=false，只暂存，点确定才真正上传）
const handleFileChange = (file: { raw?: File }) => {
  uploadFile.value = file.raw ?? null
}

const submitUpload = async () => {
  if (!uploadTarget.value) return
  if (!uploadFile.value) {
    ElMessage.warning('请先选择文件')
    return
  }
  uploading.value = true
  try {
    await uploadDocument(uploadFile.value, uploadTarget.value.id)
    uploadVisible.value = false
    ElMessage.success('已提交，正在后台入库并提炼考纲（可在列表看进度）')
    await load()
  } finally {
    uploading.value = false
  }
}

const remove = async (item: KnowledgeBaseItem) => {
  try {
    await ElMessageBox.confirm(
      `确认删除知识库「${item.name}」吗？库内 ${item.resource_count} 份资料会被解绑（文件保留、仍可用于答疑），掌握度记录会删除。`,
      '删除知识库',
      { type: 'warning' },
    )
    const result = await deleteKnowledgeBase(item.id)
    items.value = items.value.filter((row) => row.id !== item.id)
    ElMessage.success(
      `已删除：解绑 ${result.released_resource_count} 份资料、${result.released_practice_count} 场练习；` +
        `掌握度记录已删除。解绑的资料在下方「未归档文件」里，可以归档回别的库或删掉。`,
    )
    // 解绑的资料会落到「未归档资料」里，立刻刷新让用户看见它们没丢
    unboundExpanded.value = true
    await loadUnbound()
  } catch {
    // 用户取消不提示
  }
}

// ---------------------------------------------------------------- 未归档资料
const openReassign = (item: UnboundResourceItem) => {
  reassignTarget.value = item
  reassignKbId.value = items.value[0]?.id
  reassignVisible.value = true
}

const submitReassign = async () => {
  if (!reassignTarget.value || !reassignKbId.value) {
    ElMessage.warning('请选择要归档到的知识库')
    return
  }
  reassigning.value = true
  try {
    await reassignUnboundResource(reassignTarget.value.id, reassignKbId.value)
    const target = items.value.find((row) => row.id === reassignKbId.value)
    ElMessage.success(
      `已归档到「${target?.name ?? '所选知识库'}」：切片与向量本来就没被删，不会重新入库`,
    )
    reassignVisible.value = false
    reassignTarget.value = null
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '归档失败')
  } finally {
    reassigning.value = false
  }
}

const removeUnbound = async (item: UnboundResourceItem) => {
  try {
    await ElMessageBox.confirm(
      `确认彻底删除「${item.file_name}」吗？它的 ${item.chunk_count} 个切片、向量索引与原文件都会被清掉，不可撤销。`,
      '删除未归档文件',
      { type: 'warning', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    const result = await deleteUnboundResource(item.id, true)
    ElMessage.success(
      `已删除：${result.deleted_chunk_count} 个切片、${result.deleted_vector_count} 个向量` +
        (result.removed_quiz_sections > 0
          ? `，并从考纲里摘掉了 ${result.removed_quiz_sections} 个章节`
          : ''),
    )
    await loadUnbound()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  }
}

onMounted(() => {
  const tab = tabs.find((row) => row.path === route.path)
  if (tab) activeTab.value = tab.key
  void load()
})

onBeforeUnmount(() => {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
})
</script>

<template>
  <div class="kb-room">
    <AppHeader :tabs="tabs" :active-tab="activeTab" @tab-click="handleTabClick" />

    <div class="kb-room__content">
      <div class="kb-room__toolbar">
        <div class="kb-room__title">
          知识库
          <span class="kb-room__hint">
            上传资料后自动入库并提炼考纲，之后出题会按考纲范围走；点卡片可以翻库里的文档
          </span>
        </div>
        <el-button v-if="!openItem" type="primary" :loading="creating" @click="create">新建知识库</el-button>
      </div>

      <!-- 文档阅读器：左栏库内文档，右栏正文 / 切片 / 考纲 -->
      <KnowledgeDocumentReader
        v-if="openItem"
        :kb-id="openItem.id"
        :kb-name="openItem.name"
        :quiz-core="openItem.quiz_core"
        @back="openItem = null" />

      <div v-else v-loading="loading" class="kb-room__body">
        <el-empty v-if="!items.length && !loading" description="还没有知识库，先建一个吧">
          <el-button type="primary" @click="create">新建知识库</el-button>
        </el-empty>

        <div v-else class="kb-grid">
          <el-card
            v-for="item in items"
            :key="item.id"
            class="kb-card"
            shadow="hover"
            @click="enterKnowledgeBase(item)">
            <template #header>
              <div class="kb-card__head">
                <span class="kb-card__name" :title="item.name">{{ item.name }}</span>
                <el-tag :type="quizStatusView(item.quiz_status).type" size="small" effect="light">
                  {{ quizStatusView(item.quiz_status).label }}
                </el-tag>
              </div>
            </template>

            <p class="kb-card__desc">{{ item.description || '暂无说明' }}</p>

            <div class="kb-card__stats">
              <span>资料 {{ item.resource_count }} 份</span>
              <span v-if="item.ready_count" class="is-ready">就绪 {{ item.ready_count }}</span>
              <span v-if="item.pending_count" class="is-pending">处理中 {{ item.pending_count }}</span>
              <span v-if="item.failed_count" class="is-failed">失败 {{ item.failed_count }}</span>
            </div>

            <div class="kb-card__meta">
              <span>考纲提炼：{{ item.quiz_extracted_at ? formatTime(item.quiz_extracted_at) : '未完成' }}</span>
              <span v-if="isBuilding(item)" class="is-pending">构建中…</span>
            </div>

            <div class="kb-card__actions" @click.stop>
              <el-button size="small" type="primary" @click="enterKnowledgeBase(item)">查看文档</el-button>
              <el-button size="small" plain @click="openUpload(item)">上传资料</el-button>
              <el-button size="small" type="danger" plain @click="remove(item)">删除</el-button>
            </div>
          </el-card>
        </div>

        <!--
          未归档资料：删除知识库时被解绑的资料（切片、向量、原文件都还在，答疑仍检索得到）。
          以前它们没有任何入口，这里补上"看得见 + 收得回 + 能删掉"。
        -->
        <el-card v-if="unboundItems.length" class="kb-unbound" shadow="never">
          <template #header>
            <div class="kb-unbound__head" @click="unboundExpanded = !unboundExpanded">
              <span class="kb-unbound__title">
                未归档文件 {{ unboundItems.length }} 份
                <el-tag size="small" type="info" effect="plain">不属于任何知识库</el-tag>
              </span>
              <span class="kb-unbound__toggle">
                {{ unboundExpanded ? '收起' : '展开处理' }}
              </span>
            </div>
          </template>
          <p class="kb-unbound__tip">
            这些<b>文档</b>不属于任何知识库（删知识库时被解绑，或上传时没选库）：切片与向量都还在，
            <b>答疑仍会检索到它们</b>。可以归档回某个库继续出题，也可以彻底删除。
            （聊天里的图片、知识卡 png、语音这类附件不算资料，不会出现在这里。）
          </p>
          <div v-if="unboundExpanded" v-loading="unboundLoading" class="kb-unbound__list">
            <div v-for="item in unboundItems" :key="item.id" class="kb-unbound__row">
              <span class="kb-unbound__name" :title="item.file_name">{{ item.file_name }}</span>
              <span class="kb-unbound__meta">
                {{ item.chunk_count }} 个切片 · {{ item.char_count }} 字
                <template v-if="item.inactive_days !== null"> · {{ item.inactive_days }} 天前上传</template>
              </span>
              <span class="kb-unbound__ops">
                <el-button size="small" type="primary" plain @click="openReassign(item)">
                  归档到…
                </el-button>
                <el-button size="small" type="danger" plain @click="removeUnbound(item)">
                  删除
                </el-button>
              </span>
            </div>
          </div>
        </el-card>
      </div>
    </div>

    <el-dialog v-model="uploadVisible" title="上传资料到知识库" width="480px">
      <p class="upload-tip">
        目标知识库：<b>{{ uploadTarget?.name }}</b>
        <br />
        上传后会异步入库并提炼考纲，大文件也不用等。
      </p>
      <el-form label-width="86px">
        <el-form-item label="文件">
          <!--
            只收文档：图片/音频是消息附件（头像、知识卡 png、语音），它们在库内会被当成"资料"
            计数、还会混进「未归档文件」列表，所以从**选文件这一步**就把它们挡在外面
            （后端 `UploadService` 同样的口径会 400 兜底，前端只是不让用户白选一次）。
          -->
          <el-upload
            :auto-upload="false"
            :limit="1"
            :on-change="handleFileChange"
            accept=".pdf,.doc,.docx,.txt"
            drag
            class="upload-dragger">
            <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
            <div class="el-upload__text">把文件拖到这里，或<em>点击选择</em></div>
            <template #tip>
              <div class="el-upload__tip">只支持文档：PDF / Word / TXT（图片与音频不能进知识库）</div>
            </template>
          </el-upload>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submitUpload">开始上传</el-button>
      </template>
    </el-dialog>

    <!-- 未归档资料 → 归档回某个知识库 -->
    <el-dialog v-model="reassignVisible" title="归档到知识库" width="440px">
      <p class="upload-tip">
        文件：<b>{{ reassignTarget?.file_name }}</b>
        <br />
        只改归属，不会重新入库：它的切片与向量本来就没被删过。
      </p>
      <el-form label-width="72px">
        <el-form-item label="目标库">
          <el-select v-model="reassignKbId" placeholder="选择知识库" class="kb-unbound__select">
            <el-option v-for="row in items" :key="row.id" :label="row.name" :value="row.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reassignVisible = false">取消</el-button>
        <el-button type="primary" :loading="reassigning" @click="submitReassign">确定归档</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.kb-room {
  width: 100%;
  height: 100vh;
  display: flex;
  flex-direction: column;
}

.kb-room__content {
  flex: 1;
  min-height: 0;
  padding: 20px 24px;
  overflow-y: auto;
}

.kb-room__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.kb-room__title {
  font-size: 20px;
  font-weight: 700;
  color: #30353e;
}

.kb-room__hint {
  margin-left: 10px;
  font-size: 13px;
  font-weight: 400;
  color: #8a9099;
}

.kb-room__body {
  min-height: 200px;
}

.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
}

.kb-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.kb-card__name {
  font-size: 16px;
  font-weight: 600;
  color: #30353e;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-card__desc {
  min-height: 20px;
  margin: 0 0 12px;
  font-size: 13px;
  color: #6b7280;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.kb-card__stats {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 13px;
  color: #434952;

  .is-ready {
    color: #2f9e44;
  }
  .is-pending {
    color: #d9480f;
  }
  .is-failed {
    color: #c92a2a;
  }
}

.kb-card__meta {
  display: flex;
  gap: 12px;
  margin-top: 8px;
  font-size: 12px;
  color: #8a9099;
}

.kb-card__actions {
  margin-top: 14px;
  display: flex;
  gap: 8px;
}

.upload-tip {
  margin: 0 0 16px;
  font-size: 13px;
  color: #6b7280;
  line-height: 1.7;
}

/* 未归档文件（删库留下的孤儿资料）：不抢主视觉，但必须看得见、可操作 */
.kb-unbound {
  margin-top: 18px;
  border: 1px dashed #dcdfe6;
  background: #fafbfc;

  .kb-unbound__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
  }

  .kb-unbound__title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
    font-weight: 600;
    color: #434952;
  }

  .kb-unbound__toggle {
    font-size: 12px;
    color: #6b7280;
  }

  .kb-unbound__tip {
    margin: 0 0 12px;
    font-size: 12px;
    color: #8a9099;
    line-height: 1.7;
  }

  .kb-unbound__row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 0;
    border-top: 1px solid #eef0f3;
    font-size: 13px;
  }

  .kb-unbound__name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #303133;
  }

  .kb-unbound__meta {
    flex-shrink: 0;
    font-size: 12px;
    color: #8a9099;
  }

  .kb-unbound__ops {
    flex-shrink: 0;
    display: flex;
    gap: 6px;
  }
}

.kb-unbound__select {
  width: 100%;
}

.upload-dragger {
  width: 100%;
}

:deep(.el-upload),
:deep(.el-upload-dragger) {
  width: 100%;
}
</style>
