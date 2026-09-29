import { create } from 'zustand'
import client from '../api/client'

export interface UserInfo {
  username: string
  role: string
  dept_id: string
  dept_name: string
}

interface AuthState {
  user: UserInfo | null
  ready: boolean  // 2026-08-18：身份校验完成标志（verifySession 前不渲染路由，防身份闪跳）
  login: (user: UserInfo) => void
  logout: () => Promise<void>
  loadUser: (user: UserInfo) => void
  verifySession: () => Promise<boolean>
}

// user 持久化：刷新页面/HMR 后恢复（否则面包屑团队名、用户名等显示为空）
// L11：token 已改 httpOnly cookie，前端不再存储 token（XSS 无法窃取）
const savedUser = (() => {
  try {
    const raw = localStorage.getItem('user')
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
})()

export const useAuthStore = create<AuthState>((set) => ({
  user: savedUser,
  ready: false,
  login: (user) => {
    localStorage.setItem('user', JSON.stringify(user))
    set({ user, ready: true })
  },
  logout: async () => {
    // L21：登出调后端按用户吊销（ver+1，全部 token 失效）——网络失败也继续前端清理
    try {
      await client.post('/auth/logout')
    } catch {
      /* 网络异常/已失效：忽略，前端照常清理 */
    }
    localStorage.removeItem('user')
    // B6：登出清会话指向——下次登录进入 QA 页自动新建空会话（非登出的刷新仍保留）
    localStorage.removeItem('qa_current_session')
    // 2026-08-11：登出清会话列表展开状态——每次登录从默认状态开始（单次登录内刷新/离开仍保留）
    localStorage.removeItem('qa_sessions_open')
    // C1（E-01）：清 qaStore 内存态——换账号登录不得残留上一账号的会话消息/会话指向
    // （原仅清 localStorage，zustand 内存态残留 → B 账号登录直接看到 A 的完整会话）
    try {
      const { useQAStore } = await import('./qaStore')
      useQAStore.getState().reset()
    } catch { /* store 未初始化时忽略 */ }
    set({ user: null })
  },
  loadUser: (user) => set({ user }),
  // 2026-08-18：cookie 真实身份校验——localStorage user 可能与 cookie token 脱节
  // （127.0.0.1 cookie 跨端口共享：24425 部署形态与 5173 dev 并存时互相覆盖登录态；
  //  本地身份显示 A、API 实际走 B → 业务接口 403「运维管理账号不参与业务问答」/ 技能页全空）
  // 以 cookie 对应身份为准覆盖本地，401 则清身份回登录页
  verifySession: async () => {
    try {
      // 2026-09-20（走查"刷新后空页面"）：这条是**引导请求**——RequireAuth 在 ready 前 `return null`
      // ＝整页空白，而全局 axios 超时是 60s：后端重启/nginx 重载那几秒若有请求被"接住但不回"，
      // 页面就白屏最长 60 秒（用户实测"刷新几次才出来"）。这里单独收紧到 8s 并重试一次：
      // 窗口期失败 → 8s 内 flake 掉 → 重试多半已恢复；仍失败按下面 catch 走（保留本地身份继续渲染）。
      let r
      try {
        r = await client.get('/auth/me', { timeout: 8000 })
      } catch (e1) {
        if ((e1 as any)?.response?.status === 401) throw e1   // 401 不重试：身份确实失效
        await new Promise((res) => setTimeout(res, 600))
        r = await client.get('/auth/me', { timeout: 8000 })
      }
      const me = r.data?.user
      if (me) {
        localStorage.setItem('user', JSON.stringify(me))
        set({ user: me, ready: true })
        return true
      }
      set({ ready: true })
      return true
    } catch (e) {
      // 2026-09-18（@netdata A1）：只有 401 才认定身份失效——口径与 api/client.ts 拦截器一致。
      // 网络错误（断网/弱网抖一下刷新）不得清身份：原实现一律清 → 用户正常使用中刷新就被打回登录页，
      // 且 LoginPage 的 logout() 会顺手清掉 qa_current_session（会话指向一并丢失）。
      if ((e as any)?.response?.status === 401) {
        // cookie 无效/过期/被其他端口登录踢除 → 清本地身份（RequireAuth 引导回登录页）
        localStorage.removeItem('user')
        set({ user: null, ready: true })
        return false
      }
      // 服务不可达/超时：保留本地身份继续渲染，后续业务接口自行报错
      set({ ready: true })
      return true
    }
  },
}))
