import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import router from '../router'
import { clearTokens, getAccessToken, getRefreshToken, setTokens } from './token'

//const baseURL = import.meta.env.VITE_API_BASE_URL
const baseURL = '/api'

export type ApiResponse<T> = {
  code: number
  message: string
  data: T
}

// 业务接口实例：统一挂载鉴权头
const api: AxiosInstance = axios.create({
  baseURL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// 刷新专用实例：避免拦截器递归
const refreshClient = axios.create({
  baseURL,
  timeout: 30000
})

let isRefreshing = false
let pendingQueue: Array<(token?: string) => void> = []

function enqueuePending(cb: (token?: string) => void) {
  pendingQueue.push(cb)
}

function flushPending(token?: string) {
  pendingQueue.forEach((cb) => cb(token))
  pendingQueue = []
}

// 请求拦截：注入 access_token
api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = getAccessToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => {
    // 二进制响应（下载原文件走 responseType: 'blob'）没有 { code, message } 结构，
    // 直接放行，否则读 response.data.code 会拿到 undefined、把正常文件当失败。
    if (response.data instanceof Blob) {
      return response
    }
    const { code, message } = response.data as ApiResponse<unknown>
    if (typeof code === 'number' && code !== 0) {
      ElMessage.error(message || '请求失败')
      return Promise.reject(new Error(message || '请求失败'))
    }
    return response
  },
  async (error: AxiosError) => {
    const original = error.config as InternalAxiosRequestConfig & { _retry?: boolean }
    const status = error.response?.status

    // 非 401 或已重试过，直接抛出
    if (status && status !== 401) {
      // 常见状态码统一提示
      if (status === 400) ElMessage.error('请求参数错误')
      if (status === 403) ElMessage.error('没有访问权限')
      if (status === 404) ElMessage.error('资源不存在')
      if (status === 409) ElMessage.error('资源冲突')
      if (status === 422) ElMessage.error('请求参数校验失败')
      if (status === 500) ElMessage.error('系统异常，请稍后再试')
      return Promise.reject(error)
    }

    if (status !== 401 || original?._retry) {
      return Promise.reject(error)
    }

    const refreshToken = getRefreshToken()
    if (!refreshToken) {
      // 没有 refresh_token，清理本地并抛出
      clearTokens()
      router.replace('/login')
      return Promise.reject(error)
    }

    if (isRefreshing) {
      // 正在刷新：将请求挂起，等刷新完成后重试
      return new Promise((resolve, reject) => {
        enqueuePending((token) => {
          if (!token) {
            reject(error)
            return
          }
          original.headers.Authorization = `Bearer ${token}`
          resolve(api(original))
        })
      })
    }

    original._retry = true
    isRefreshing = true

    try {
      // 根据接口文档：刷新请求体字段名为 "refresh token"
      const refreshResp = await refreshClient.post<ApiResponse<{ access_token: string; refresh_token: string; expires_in?: number }>>('/auth/refresh', {
        'refresh token': refreshToken
      })
      const { code, message, data } = refreshResp.data
      if (code !== 0) {
        throw new Error(message || 'Invalid refresh response')
      }
      if (!data?.access_token || !data?.refresh_token) {
        throw new Error('Invalid refresh response')
      }
      // 保存新 token，并重放挂起请求
      setTokens(data)
      flushPending(data.access_token)
      original.headers.Authorization = `Bearer ${data.access_token}`
      return api(original)
    } catch (refreshError) {
      // 刷新失败：清理本地并拒绝所有挂起请求
      clearTokens()
      flushPending(undefined)
      router.replace('/login')
      return Promise.reject(refreshError)
    } finally {
      isRefreshing = false
    }
  }
)

export default api
