import api, { type ApiResponse } from './http'

export type LoginRequest = {
  username: string
  password: string
}

export type RegisterRequest = {
  username: string
  password: string
}

export type TokenResponse = {
  access_token: string
  refresh_token: string
  token_type?: string
  expires_in?: number
}

// 登录
export async function login(payload: LoginRequest) {
  const { data: { data } } = await api.post<ApiResponse<TokenResponse>>('/auth/login', payload)
  return data
}

// 注册
export async function register(payload: RegisterRequest) {
  const { data: { data } } = await api.post<ApiResponse<unknown>>('/auth/register', payload)
  return data
}
