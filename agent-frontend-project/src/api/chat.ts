import api, { type ApiResponse } from './http'
import type { SessionListResponse, SessionMessagesResponse } from '@/types/chat'

// 会话列表：按会话模式（学习/笔记）分页查询。
export async function getSessions(params: { page: number; page_size: number; session_model: 0 | 2 }) {
  const { data: { data } } = await api.get<ApiResponse<SessionListResponse>>('/sessions', { params })
  return data
}

// 创建会话主题。
export async function createSession(payload: { title: string; session_model: number }) {
  const { data: { data } } = await api.post<ApiResponse<{ message: string; session: SessionListResponse['sessions'][number] }>>(
    '/sessions',
    payload
  )
  return data
}

// 编辑会话标题；后端会校验 session_model 与会话归属。
export async function updateSessionTitle(
  sessionId: number,
  payload: { title: string },
  sessionModel: 0 | 2
) {
  const { data: { data } } = await api.put<ApiResponse<{ message: string; session: SessionListResponse['sessions'][number] }>>(
    `/sessions/${sessionId}`,
    payload,
    { params: { session_model: sessionModel } }
  )
  return data
}

// 删除会话，并联动删除该会话的聊天消息/资源。
export async function deleteSession(sessionId: number, sessionModel: 0 | 2) {
  const { data: { data } } = await api.delete<ApiResponse<{
    message: string
    session_id: number
    deleted_messages: number
    deleted_resources: number
  }>>(`/sessions/${sessionId}`, { params: { session_model: sessionModel } })
  return data
}

// 按会话分页拉取聊天记录（正文 + 附件段）。
export async function getSessionMessages(
  sessionId: number,
  params: { page: number; page_size: number; session_model: 0 | 2 }
) {
  const { data: { data } } = await api.get<ApiResponse<SessionMessagesResponse>>(`/sessions/${sessionId}/messages`, { params })
  return data
}
