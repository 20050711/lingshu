// 功能问答页（核心）：对话区 + 预览浏览区（可拖拽分隔条 280~800px）
// 对照 prototype-v1.1-rev2.html：无顶部工具栏，控件全在输入框下方
import { useEffect, useRef } from 'react'
import { useQAStore } from '../stores/qaStore'
import ChatPanel from './qa/ChatPanel'
import PreviewPanel from './qa/PreviewPanel'

export default function QAPage() {
  const previewOpen = useQAStore((s) => s.previewOpen)
  const loadSessions = useQAStore((s) => s.loadSessions)
  const newSession = useQAStore((s) => s.newSession)
  const loadSkillPrefs = useQAStore((s) => s.loadSkillPrefs)
  const initialized = useRef(false)

  useEffect(() => {
    if (initialized.current) return
    initialized.current = true
    loadSkillPrefs() // 三期 M15：恢复用户技能勾选（不阻塞会话加载）
    loadSessions().then(() => {
      // B6：登录/登出已清 localStorage sessionId → 无 sessionId 且**确实没有历史会话**时才新建
      // （2026-08-18 修复：原无 sessionId 一律新建 → 每次登录产生一个空会话并保留堆积；
      // 有历史会话时保持空态，发送消息自动建——sendQuestion 已有 !sessionId 自动建逻辑）
      const { sessionId, sessions } = useQAStore.getState()
      if (!sessionId && !sessions.length) newSession()
    })
  }, [])

  return (
    <div style={{ display: 'flex', height: '100%', overflow: 'hidden', background: 'var(--surface-1)', borderRadius: 8, border: '1px solid var(--border)' }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <ChatPanel />
      </div>
      {previewOpen && <PreviewPanel />}
    </div>
  )
}
