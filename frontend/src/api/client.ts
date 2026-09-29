import axios from 'axios'
import { API_PREFIX } from './prefix'

// axios 拦截器：X-Client-ID + 错误码映射 + 401 跳登录
// L11（2026-08-06）：token 改 httpOnly cookie（同源经 /api 代理自动携带），前端不再接触 token
export const getClientId = () => {
  let id = localStorage.getItem('client_id')
  if (!id) {
    // crypto.randomUUID 仅在 secure context（https/localhost）可用；
    // 局域网 http 部署（如 http://192.168.x.x:24425）下须降级生成
    id = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `cid-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
    localStorage.setItem('client_id', id)
  }
  return id
}

const client = axios.create({ baseURL: API_PREFIX, timeout: 60000 })

client.interceptors.request.use((config) => {
  config.headers['X-Client-ID'] = getClientId()
  return config
})

// 401（E006）统一处理：清本地登录态 + 跳登录页——axios 拦截器与 SSE fetch（发现 17）共用
export function handleAuthExpired() {
  localStorage.removeItem('user')
  location.href = '/login'
}

client.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const status = error.response?.status
    const errCode = error.response?.data?.error?.code
    if (status === 401 && errCode === 'E006' && !location.pathname.startsWith('/login')) {
      handleAuthExpired()
    }
    return Promise.reject(error)
  },
)

export function errMessage(error: unknown): string {
  const detail = (error as any)?.response?.data?.error
  return detail?.message || '请求失败，请稍后重试'
}

export default client
