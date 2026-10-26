import http, { type ApiResponse } from './http'
import { getAccessToken } from './token'

// 定义从 JWT 载荷中解码出的用户信息结构
interface DecodedToken {
  sub: string // subject, 通常是 user_id
  // 其他你可能关心的字段...
}

// 定义从 /users/{id} 接口返回的用户信息结构
export interface UserProfile {
  id: number
  username: string
  avatar: string | null
}

/**
 * 从 Base64 编码的 JWT 令牌中解码出载荷信息。
 * 注意：这只做解码，不做签名验证。签名验证应由后端完成。
 */
function decodeToken(token: string): DecodedToken | null {
  try {
    const payloadBase64 = token.split('.')[1]
    if (!payloadBase64) return null
    // atob 已被许多环境弃用，但浏览器中仍然广泛支持
    // 在 Node.js 环境中，需要使用 Buffer.from(payloadBase64, 'base64').toString('utf8')
    const decodedPayload = atob(payloadBase64)
    return JSON.parse(decodedPayload) as DecodedToken
  } catch (error) {
    console.error('Failed to decode token:', error)
    return null
  }
}

/**
 * 获取当前登录用户的个人信息
 */
export async function getMyProfile(): Promise<UserProfile | null> {
  const token = getAccessToken()
  if (!token) {
    console.warn('No access token found, cannot fetch user profile.')
    return null
  }

  const decoded = decodeToken(token)
  const userId = decoded?.sub

  if (!userId) {
    console.error('Could not find user ID in token.')
    return null
  }

  try {
    const { data: { data } } = await http.get<ApiResponse<UserProfile>>(`/users/${userId}`)
    return data
  } catch (error) {
    console.error('Failed to fetch user profile:', error)
    return null
  }
}
