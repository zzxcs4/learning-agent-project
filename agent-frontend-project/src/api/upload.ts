
import api, { type ApiResponse } from './http'

// 头像上传接口的响应结构
export interface UploadAvatarResponse {
  message: string
  avatar_url: string | null
}

/**
 * 上传用户头像
 * upload_purpose=1 表示头像用途，storage_scene=3 表示头像存储场景
 */
export async function uploadAvatar(file: File): Promise<UploadAvatarResponse> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('upload_purpose', '1')
  formData.append('storage_scene', '3')

  const { data: { data } } = await api.post<ApiResponse<UploadAvatarResponse>>(
    '/upload/file',
    formData,
    {
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    }
  )
  return data
}

// 文档上传接口的响应结构
export interface UploadDocumentResponse {
  message: string
  resource_id: number // 资源 ID，后续业务操作需用到
  file_path: string
  file_hash: string
  resource_type: number
  storage_scene: number
  upload_purpose: number
  expire_time: string | null
  is_deduplicated: boolean // 是否命中去重（命中则未重复存储）
  avatar_url: string | null
}

/**
 * 上传业务文档（PDF / Word / TXT 等）
 * upload_purpose=0 表示文档用途，storage_scene=0 表示文档存储场景
 *
 * 传了 `knowledgeBaseId` 时，后端会异步完成"向量入库 + 考纲提炼"，
 * 上传接口本身只做存文件与建记录，因此立即返回；进度看知识库的 quiz_status。
 */
export async function uploadDocument(
  file: File,
  knowledgeBaseId?: number
): Promise<UploadDocumentResponse> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('upload_purpose', '0')
  formData.append('storage_scene', '0')
  if (knowledgeBaseId) {
    formData.append('knowledge_base_id', String(knowledgeBaseId))
  }

  const { data: { data } } = await api.post<ApiResponse<UploadDocumentResponse>>(
    '/upload/file',
    formData,
    {
      headers: {
        'Content-Type': 'multipart/form-data'
      },
      // 大文件入库在后台跑，但上传本身仍可能传较久，给足超时
      timeout: 120000
    }
  )
  return data
}
