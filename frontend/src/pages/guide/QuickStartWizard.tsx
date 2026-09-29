/**
 * 快速上手向导：3 步走（新建会话 → 提问 → 看结果），小白零基础起步。
 */
import { useEffect, useState } from 'react'
import { CheckCircle, ChatCircle, Confetti, Lightbulb, Plus, type Icon as PhosphorIcon } from '@phosphor-icons/react'
import Icon from '../../components/Icon'

// 2026-09-18：图标由 emoji 换成 Phosphor Light（存组件本体，渲染处 <Icon as={...} />）
const STEPS: { icon: PhosphorIcon; title: string; desc: string; point: string }[] = [
  {
    icon: Plus,
    title: '新建会话',
    desc: '左侧导航点「智能助手」，然后点「+ 新建会话」。',
    point: '新会话 = 一段独立对话，不同主题分开问更清晰。',
  },
  {
    icon: ChatCircle,
    title: '提问',
    desc: '在底部输入框用大白话输入需求，按回车发送。',
    point: '例如："市场部有哪些表？"——不用会 SQL，说人话就行。',
  },
  {
    icon: CheckCircle,
    title: '看结果',
    desc: 'AI 回复正文在中间，表格/图表/文件在右侧预览区。',
    point: '需要下载的文件点预览区文件即可。就这么简单，开始用吧！',
  },
]

export default function QuickStartWizard() {
  const [idx, setIdx] = useState(0)
  const [entered, setEntered] = useState(false)

  useEffect(() => {
    setEntered(true)
  }, [idx])

  const go = (n: number) => {
    setEntered(false)
    setTimeout(() => setIdx(Math.min(Math.max(n, 0), STEPS.length - 1)), 120)
  }

  const s = STEPS[idx]

  return (
    <div className="gd-wizard">
      <div className="gd-wizard-progress">
        {STEPS.map((_, i) => (
          <span key={i} className={`gd-wizard-dot ${i === idx ? 'gd-dot-active' : i < idx ? 'gd-dot-done' : ''}`} onClick={() => go(i)} />
        ))}
        <span className="gd-wizard-stepno">第 {idx + 1} / {STEPS.length} 步</span>
      </div>
      <div className={`gd-wizard-body ${entered ? 'gd-enter' : ''}`}>
        <div className="gd-wizard-icon"><Icon as={s.icon} size={34} /></div>
        <h3>{s.title}</h3>
        <p>{s.desc}</p>
        <div className="gd-wizard-point"><Icon as={Lightbulb} size={13} /> {s.point}</div>
      </div>
      <div className="gd-wizard-nav">
        <button className="gd-btn gd-btn-ghost" disabled={idx === 0} onClick={() => go(idx - 1)}>上一步</button>
        {idx < STEPS.length - 1 ? (
          <button className="gd-btn gd-btn-primary" onClick={() => go(idx + 1)}>下一步 →</button>
        ) : (
          <span className="gd-wizard-done"><Icon as={Confetti} /> 你已经学会基本操作了！</span>
        )}
      </div>
    </div>
  )
}
