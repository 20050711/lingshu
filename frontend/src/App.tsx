import { useEffect } from 'react'
import { BrowserRouter } from 'react-router-dom'
import AppRouter from './router'
import { useAuthStore } from './stores/authStore'

export default function App() {
  // 2026-08-18：App 根挂载即校验 cookie 真实身份（localStorage 身份可能与 cookie 脱节——
  // 127.0.0.1 cookie 跨端口共享，24425/5173 并存时互相覆盖登录态；必须在路由守卫前完成）
  useEffect(() => { useAuthStore.getState().verifySession() }, [])
  return (
    // SEC-20（2026-08-17）：react-router 7 默认启用 v7 行为，future 标志已移除
    <BrowserRouter>
      <AppRouter />
    </BrowserRouter>
  )
}
