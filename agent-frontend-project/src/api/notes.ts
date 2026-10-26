import api, { type ApiResponse } from './http'

/**
 * 笔记接口：对应后端 app/routes/note_routes.py。
 * 注意 `content` 约定为 **Markdown**（AI 归档写的也是 Markdown），前端按 Markdown 编辑与预览。
 */

export interface NoteListItem {
  id: number
  note_type: string | null
  title: string
  created_at: number
  updated_at: number
}

export interface NoteItem extends NoteListItem {
  user_id: number
  session_id: number | null
  source_run_id: string | null
  content: string
  tags: string[]
  metadata: Record<string, unknown>
}

export interface NoteListData {
  page: number
  page_size: number
  total: number
  has_more: boolean
  notes: NoteListItem[]
}

export interface NoteListQuery {
  page?: number
  page_size?: number
  note_type?: string
  keyword?: string
}

/** 分页获取笔记列表（按更新时间倒序，后端保证） */
export async function listNotes(query: NoteListQuery = {}): Promise<NoteListData> {
  const {
    data: { data },
  } = await api.get<ApiResponse<NoteListData>>('/notes', {
    params: { page: 1, page_size: 20, ...query },
  })
  return data
}

/** 单篇笔记详情（含 Markdown 正文） */
export async function getNote(id: number): Promise<NoteItem> {
  const {
    data: { data },
  } = await api.get<ApiResponse<{ note: NoteItem }>>(`/notes/${id}`)
  return data.note
}

/** 新建笔记 */
export async function createNote(payload: {
  title: string
  content: string
  note_type?: string
  tags?: string[]
}): Promise<NoteItem> {
  const {
    data: { data },
  } = await api.post<ApiResponse<{ note: NoteItem }>>('/notes', payload)
  return data.note
}

/** 更新笔记（只传要改的字段） */
export async function updateNote(
  id: number,
  payload: { title?: string; content?: string; note_type?: string; tags?: string[] },
): Promise<NoteItem> {
  const {
    data: { data },
  } = await api.put<ApiResponse<{ note: NoteItem }>>(`/notes/${id}`, payload)
  return data.note
}

/** 删除笔记 */
export async function deleteNote(id: number): Promise<{ message: string; note_id: number }> {
  const {
    data: { data },
  } = await api.delete<ApiResponse<{ message: string; note_id: number }>>(`/notes/${id}`)
  return data
}
