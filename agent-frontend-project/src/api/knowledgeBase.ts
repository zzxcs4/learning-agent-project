import api, { type ApiResponse } from './http'

/** 考纲提炼状态：与后端 knowledge_bases.quiz_status 一致 */
export type QuizStatus = 'pending' | 'extracting' | 'ready' | 'empty' | 'failed'

/** 考纲章节：`source_ref.chunk_from/to` 用来跳回原文片段 */
export interface QuizCoreSection {
  heading?: string
  topics?: string[]
  source_ref?: { resource_id?: number; chunk_from?: number; chunk_to?: number }
  /**
   * 原文资料还在不在（**后端详情接口现算的**，不落库）：
   * `ok` 在 / `missing` 已被删除 / `unknown` 历史数据没有可用 resource_id。
   */
  source_status?: 'ok' | 'missing' | 'unknown'
  asked_count?: number
  last_asked_at?: string
}

export interface QuizCore {
  title?: string
  sections?: QuizCoreSection[]
  frequently_tested?: string[]
  followup_paths?: string[]
}

/** 知识库列表项：带构建状态，用于画"准备中 / 就绪 / 失败" */
export interface KnowledgeBaseItem {
  id: number
  name: string
  description: string | null
  resource_count: number
  ready_count: number
  failed_count: number
  pending_count: number
  quiz_status: QuizStatus
  quiz_extracted_at: number | null
  can_quiz: boolean
  can_retrieve: boolean
  created_at: number | null
  /** 只有详情接口返回考纲（列表里为 null），阅读器用它画"考纲 → 跳原文" */
  quiz_core: QuizCore | null
}

export interface KnowledgeBaseListData {
  total: number
  items: KnowledgeBaseItem[]
}

export interface KnowledgeBaseDeleteData {
  knowledge_base_id: number
  /** 被解绑的资料数：切片、向量、原文件都保留，答疑仍检索得到 */
  released_resource_count: number
  /** 解绑的练习场次数（历史保留） */
  released_practice_count: number
  /** 一并删除的掌握度记录数（掌握度的键就是知识库，库没了键就没了） */
  removed_mastery_count: number
  /**
   * 保留策略的机器可读摘要（分号分隔），删库前想弄清"什么还在"就读它，不要去猜：
   * `resources_unbound` / `practice_history_kept` / `weak_points_generalized` / `mastery_removed`
   */
  retention_policy: string
}

/** 知识库列表（后端按创建时间倒序） */
export async function listKnowledgeBases(): Promise<KnowledgeBaseListData> {
  const {
    data: { data },
  } = await api.get<ApiResponse<KnowledgeBaseListData>>('/knowledge-bases')
  return data
}

/** 新建知识库 */
export async function createKnowledgeBase(payload: {
  name: string
  description?: string
}): Promise<KnowledgeBaseItem> {
  const {
    data: { data },
  } = await api.post<ApiResponse<KnowledgeBaseItem>>('/knowledge-bases', payload)
  return data
}

/** 单个知识库详情（含构建状态） */
export async function getKnowledgeBase(id: number): Promise<KnowledgeBaseItem> {
  const {
    data: { data },
  } = await api.get<ApiResponse<KnowledgeBaseItem>>(`/knowledge-bases/${id}`)
  return data
}

/** 更新知识库名称 / 说明 */
export async function updateKnowledgeBase(
  id: number,
  payload: { name?: string; description?: string | null },
): Promise<KnowledgeBaseItem> {
  const {
    data: { data },
  } = await api.patch<ApiResponse<KnowledgeBaseItem>>(`/knowledge-bases/${id}`, payload)
  return data
}

/**
 * 删除知识库。
 * 后端语义：资料与练习解绑、掌握度记录删除、弱点改回通用；不删文件与向量。
 */
export async function deleteKnowledgeBase(id: number): Promise<KnowledgeBaseDeleteData> {
  const {
    data: { data },
  } = await api.delete<ApiResponse<KnowledgeBaseDeleteData>>(`/knowledge-bases/${id}`)
  return data
}

/** 状态文案与颜色，列表与详情共用 */
export function quizStatusView(status: QuizStatus): { label: string; type: '' | 'success' | 'warning' | 'danger' | 'info' } {
  switch (status) {
    case 'ready':
      return { label: '考纲就绪', type: 'success' }
    case 'extracting':
      return { label: '考纲提炼中', type: 'warning' }
    case 'empty':
      return { label: '无可提炼内容', type: 'info' }
    case 'failed':
      return { label: '提炼失败', type: 'danger' }
    default:
      return { label: '待提炼', type: 'info' }
  }
}

/** 是否还在构建中（用于轮询） */
export function isBuilding(item: KnowledgeBaseItem): boolean {
  return (
    item.pending_count > 0 ||
    item.quiz_status === 'pending' ||
    item.quiz_status === 'extracting'
  )
}

// ============================================================================
// 库内文档（阅读器）
// ============================================================================

/** 资料入库状态：与后端 resources.ingest_status 一致 */
export type IngestStatus = 'pending' | 'ingesting' | 'ready' | 'failed'

/** 库内一份文档（列表项，不含正文） */
export interface ResourceItem {
  id: number
  file_name: string
  group_name: string | null
  ingest_status: IngestStatus
  ingest_error: string | null
  chunk_count: number
  char_count: number
  has_text: boolean
  can_preview: boolean
  create_time: number | null
  /** 搜索时：该文档命中的片段数（非搜索为 0） */
  matched_chunk_count: number
  /** 搜索时：命中位置 name / content / both；非搜索为 null */
  match_field: 'name' | 'content' | 'both' | null
  /** 搜索时：命中的正文片段（最多 3 条） */
  match_snippets: string[]
}

export interface ResourceListData {
  knowledge_base_id: number
  total: number
  /** 本次搜索关键词（原样回显） */
  keyword: string | null
  /** 本次分组过滤条件（原样回显） */
  group_name: string | null
  items: ResourceItem[]
}

/** 库内分组：`group_name` 是纯字符串列，分组由数据反推，没有分组表 */
export interface ResourceGroupItem {
  name: string | null
  resource_count: number
}

export interface ResourceGroupListData {
  knowledge_base_id: number
  total: number
  items: ResourceGroupItem[]
}

/** 删除单份文档的结果 */
export interface ResourceDeleteData {
  resource_id: number
  file_name: string
  knowledge_base_id: number | null
  deleted_chunk_count: number
  deleted_vector_count: number
  deleted_file: boolean
  /** 同时从考纲里摘掉的章节数（章节以这份资料为原文锚点，资料没了就定位不到原文） */
  removed_quiz_sections: number
}

export interface ResourceChunk {
  chunk_index: number
  char_count: number
  text: string
}

/** 正文来源：original=入库时存的解析全文；chunks=用切片拼回；none=读不到 */
export type ResourceTextSource = 'original' | 'chunks' | 'none'

export interface ResourceDetail extends Omit<ResourceItem, 'has_text'> {
  knowledge_base_id: number | null
  text: string
  text_chars: number
  text_source: ResourceTextSource
  chunks: ResourceChunk[]
  /** 原文件的服务端路径；**要带令牌**请求（用 fetchResourceFile），不能当链接直接打开 */
  preview_path: string | null
}

/**
 * 库内文档列表（按上传时间倒序）。
 *
 * `keyword` 是**库内搜索**：文件名或切片正文命中即返回，结果带
 * `match_field` / `matched_chunk_count` / `match_snippets` 说明命中原因。
 */
export async function listKnowledgeBaseResources(
  kbId: number,
  options: { keyword?: string; groupName?: string } = {},
): Promise<ResourceListData> {
  const params: Record<string, string> = {}
  const keyword = options.keyword?.trim()
  const groupName = options.groupName?.trim()
  if (keyword) params.keyword = keyword
  if (groupName) params.group_name = groupName
  const {
    data: { data },
  } = await api.get<ApiResponse<ResourceListData>>(`/knowledge-bases/${kbId}/resources`, {
    params: Object.keys(params).length ? params : undefined,
  })
  return data
}

/** 库内分组（含未分组） */
export async function listKnowledgeBaseGroups(kbId: number): Promise<ResourceGroupListData> {
  const {
    data: { data },
  } = await api.get<ApiResponse<ResourceGroupListData>>(`/knowledge-bases/${kbId}/groups`)
  return data
}

/**
 * 文档改名 / 移动分组。
 *
 * `groupName` 传空字符串表示**移出分组**，不传表示不改动（后端用 `exclude_unset` 区分）。
 */
export async function updateKnowledgeBaseResource(
  kbId: number,
  resourceId: number,
  payload: { fileName?: string; groupName?: string },
): Promise<ResourceDetail> {
  const body: Record<string, string> = {}
  if (payload.fileName !== undefined) body.file_name = payload.fileName
  if (payload.groupName !== undefined) body.group_name = payload.groupName
  const {
    data: { data },
  } = await api.patch<ApiResponse<ResourceDetail>>(
    `/knowledge-bases/${kbId}/resources/${resourceId}`,
    body,
  )
  return data
}

/**
 * 删除单份文档。**不可撤销**：后端会一并删掉切片、Qdrant 向量与原文件。
 *
 * `deleteFile=false` 只把文档移出知识库，对象存储里的原文件保留（仍可用于答疑检索）。
 */
export async function deleteKnowledgeBaseResource(
  kbId: number,
  resourceId: number,
  deleteFile = true,
): Promise<ResourceDeleteData> {
  const {
    data: { data },
  } = await api.delete<ApiResponse<ResourceDeleteData>>(
    `/knowledge-bases/${kbId}/resources/${resourceId}`,
    { params: deleteFile ? undefined : { delete_file: false } },
  )
  return data
}

/** 文档详情：正文 + 全部切片 + 预览地址 */
export async function getKnowledgeBaseResource(
  kbId: number,
  resourceId: number,
): Promise<ResourceDetail> {
  const {
    data: { data },
  } = await api.get<ApiResponse<ResourceDetail>>(
    `/knowledge-bases/${kbId}/resources/${resourceId}`,
  )
  return data
}

/** 单个切片：考纲的 source_ref.chunk_from 直接喂进来就能定位 */
export async function getKnowledgeBaseResourceChunk(
  kbId: number,
  resourceId: number,
  chunkIndex: number,
): Promise<{ resource_id: number; resource_name: string; chunk: ResourceChunk }> {
  const {
    data: { data },
  } = await api.get<
    ApiResponse<{ resource_id: number; resource_name: string; chunk: ResourceChunk }>
  >(`/knowledge-bases/${kbId}/resources/${resourceId}/chunks/${chunkIndex}`)
  return data
}

/**
 * 取原文件。
 *
 * 必须**带令牌**走 axios：后端用 `Authorization` 鉴权，直接开新标签页会 401。
 * 后端之所以做代理而不是给 COS 预签名链接：2024 年后创建的桶用 COS 默认域名
 * 访问一律被强制下载，无法内联预览 PDF。
 */
export async function fetchResourceFile(
  kbId: number,
  resourceId: number,
  download = false,
): Promise<{ blob: Blob; filename: string }> {
  const response = await api.get(`/knowledge-bases/${kbId}/resources/${resourceId}/file`, {
    params: download ? { download: true } : undefined,
    responseType: 'blob',
    timeout: 120000,
  })
  const blob = response.data as Blob
  const disposition = String(response.headers['content-disposition'] || '')
  // 优先用 RFC 5987 的 filename*（中文名不会乱码），退回 ASCII 兜底名
  const star = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
  const plain = /filename="?([^";]+)"?/i.exec(disposition)
  let filename = `resource-${resourceId}`
  if (star && star[1]) {
    try {
      filename = decodeURIComponent(star[1])
    } catch {
      filename = star[1]
    }
  } else if (plain && plain[1]) {
    filename = plain[1]
  }
  return { blob, filename }
}

/**
 * 未归档资料：删除知识库时被**解绑**（不是删除）的资料。
 *
 * 它们的切片、向量、原文件都还在，答疑仍检索得到；而库内文档列表必须传库 ID，
 * 所以这类资料此前在界面上没有任何入口。下面两个接口补上"看得见 + 收得回"。
 */
export interface UnboundResourceItem {
  id: number
  file_name: string
  group_name: null
  knowledge_base_id: null
  ingest_status: IngestStatus
  ingest_error: string | null
  chunk_count: number
  char_count: number
  has_text: boolean
  can_preview: boolean
  create_time: number | null
  /** 距上传过了多少天（用来提示「注意：它已经很久没被用过了」） */
  inactive_days: number | null
}

/** 列出未归档资料（用户级，不按库过滤） */
export async function listUnboundResources(): Promise<{
  total: number
  items: UnboundResourceItem[]
}> {
  const {
    data: { data },
  } = await api.get<ApiResponse<{ total: number; items: UnboundResourceItem[] }>>(
    '/knowledge-bases/unbound-resources',
  )
  return data
}

/** 把未归档资料重新归档到某个知识库（纯元数据变更：切片与向量本来就没被删过） */
export async function reassignUnboundResource(
  resourceId: number,
  knowledgeBaseId: number,
): Promise<ResourceDetail> {
  const {
    data: { data },
  } = await api.patch<ApiResponse<ResourceDetail>>(
    `/knowledge-bases/unbound-resources/${resourceId}`,
    { knowledge_base_id: knowledgeBaseId },
  )
  return data
}

/**
 * 彻底删除一份未归档资料（切片、向量、原文件一起清）。
 *
 * 未归档资料不属于任何库，所以不能走库内删除接口（那条路由要传库 ID，
 * 而随便传一个库 ID 等于绕过归属校验）。
 */
export async function deleteUnboundResource(
  resourceId: number,
  deleteFile = true,
): Promise<ResourceDeleteData> {
  const {
    data: { data },
  } = await api.delete<ApiResponse<ResourceDeleteData>>(
    `/knowledge-bases/unbound-resources/${resourceId}`,
    { params: deleteFile ? undefined : { delete_file: false } },
  )
  return data
}

/** 入库状态 → 文案与颜色 */
export function ingestStatusView(status: IngestStatus): {
  label: string
  type: '' | 'success' | 'warning' | 'danger' | 'info'
} {
  switch (status) {
    case 'ready':
      return { label: '已入库', type: 'success' }
    case 'ingesting':
      return { label: '入库中', type: 'warning' }
    case 'failed':
      return { label: '入库失败', type: 'danger' }
    default:
      return { label: '排队中', type: 'info' }
  }
}
