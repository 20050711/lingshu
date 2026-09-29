// 账号设置 Modal（2026-09-01）：修改账号名（可选）+ 修改密码（可选，可同时）
// 提交 PUT /auth/account——后端一次事务完成并 token_version+1 吊销全部旧会话，
// 成功提示后走 logout 清理（服务端调用 401 被吞，前端清理照常执行）再跳登录页
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Input, Modal, message } from 'antd'
import client from '../api/client'
import { useAuthStore } from '../stores/authStore'

export default function AccountModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [oldPwd, setOldPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [confirmPwd, setConfirmPwd] = useState('')
  const [saving, setSaving] = useState(false)

  const resetForm = () => {
    setUsername('')
    setOldPwd('')
    setNewPwd('')
    setConfirmPwd('')
  }

  const submit = async () => {
    const wantName = !!username.trim() && username.trim() !== user?.username
    const wantPwd = !!(oldPwd || newPwd || confirmPwd)
    if (!wantName && !wantPwd) { message.warning('没有需要修改的内容'); return }
    if (wantName && (username.trim().includes(' ') || username.trim().length > 50)) {
      message.warning('账号名不能含空格，最长 50 字'); return
    }
    if (wantPwd) {
      if (!oldPwd || !newPwd || !confirmPwd) { message.warning('请填写完整：原密码 / 新密码 / 确认新密码'); return }
      if (newPwd.length < 8 || newPwd.length > 16) { message.warning('新密码需 8-16 位'); return }
      if (newPwd !== confirmPwd) { message.warning('两次输入的新密码不一致'); return }
    }
    setSaving(true)
    try {
      await client.put('/auth/account', {
        new_username: wantName ? username.trim() : null,
        old_password: wantPwd ? oldPwd : null,
        new_password: wantPwd ? newPwd : null,
      })
      message.success('已保存，请用新账号密码重新登录')
      resetForm()
      onClose()
      await logout()  // token 已吊销：服务端 401 被吞，前端清理照常
      navigate('/login')
    } catch (e: any) {
      message.error(e?.response?.data?.error?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal title="修改账号密码" open={open} confirmLoading={saving} onCancel={() => { resetForm(); onClose() }} onOk={submit}
      okText="保存并重新登录" width={440}>
      <div style={{ fontSize: 12, color: '#888', marginBottom: 12 }}>
        修改账号名和/或密码，保存后需重新登录；密码 8-16 位。
      </div>
      {/* 2026-09-01：只读展示当前账号（防改错源账号）+ 新账号名（留空不修改） */}
      <Input style={{ marginBottom: 10 }} value={user?.username ?? ''} disabled addonBefore="当前账号" />
      <Input style={{ marginBottom: 10 }} placeholder="新账号名（留空不修改）"
        value={username} onChange={(e) => setUsername(e.target.value)} maxLength={50} />
      <Input.Password style={{ marginBottom: 10 }} placeholder="原密码" autoComplete="current-password"
        value={oldPwd} onChange={(e) => setOldPwd(e.target.value)} />
      <Input.Password style={{ marginBottom: 10 }} placeholder="新密码（8-16 位）" autoComplete="new-password"
        value={newPwd} onChange={(e) => setNewPwd(e.target.value)} />
      <Input.Password placeholder="确认新密码" autoComplete="new-password"
        value={confirmPwd} onChange={(e) => setConfirmPwd(e.target.value)} />
    </Modal>
  )
}
