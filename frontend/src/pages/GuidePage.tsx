/**
 * 使用说明页：快速上手向导 + 功能卡片（Bento 网格，点击弹浮窗教学）+ 常见问题。
 * 面向零基础用户：每步教学到"在哪点、点什么、会看到什么"；示例可一键真实试用。
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { BookOpen, Books, ListChecks, PlayCircle, Question, Rocket, Warning } from '@phosphor-icons/react'
import Icon from '../components/Icon'
import { FEATURES, GROUPS } from './guide/features'
import type { Feature } from './guide/features'
import FeatureCard from './guide/FeatureCard'
import SimDemo from './guide/SimDemo'
import NoticeCards from './guide/NoticeCards'
import QuickStartWizard from './guide/QuickStartWizard'
import FaqSection from './guide/FaqSection'
import './guide/guide.css'

export default function GuidePage() {
  const navigate = useNavigate()
  const [openFeature, setOpenFeature] = useState<Feature | null>(null)
  const [activeGroup, setActiveGroup] = useState<string>(GROUPS[0])
  // 2026-09-17：CEO 看板卡已随数据查询线下线删除（此处不再按角色过滤）
  const features = FEATURES

  const tryIt = (question: string) => {
    setOpenFeature(null)
    // C9（2026-08-12）：暂存示例问题——QA 页挂载预填输入框（原 navigate 丢弃入参）
    if (question) localStorage.setItem('qa_pending_question', question)
    navigate('/qa')
  }

  // 入场动画：IntersectionObserver 滚动淡入（activeGroup 变化后重新观察新渲染的卡片）
  useEffect(() => {
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            e.target.classList.add('gd-reveal-in')
            io.unobserve(e.target)
          }
        })
      },
      { threshold: 0.08 }
    )
    const els = document.querySelectorAll('.gd-reveal:not(.gd-reveal-in)')
    els.forEach((el) => io.observe(el))
    return () => io.disconnect()
  }, [activeGroup])

  // ESC 关闭浮窗
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpenFeature(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const grouped = GROUPS.map((g) => ({ group: g, items: features.filter((f) => f.group === g) })).filter((g) => g.items.length > 0)

  return (
    <div className="gd-page">
      {/* 顶栏：返回首页 */}
      <div className="gd-topbar">
        <button className="gd-btn gd-btn-ghost" onClick={() => navigate('/home')}>← 返回首页</button>
        <span className="gd-topbar-hint"><Icon as={BookOpen} size={13} /> 使用说明 · 三分钟学会平台</span>
      </div>

      {/* 顶部欢迎区 */}
      <section className="gd-hero gd-reveal">
        <span className="gd-eyebrow">使用指南</span>
        <h1>三分钟学会用平台</h1>
        <p>这个页面会一步一步教你：从登录开始，到查数据、画图表、生成报告。跟着做就行，遇到不懂的点击卡片查看完整教学。</p>
      </section>

      {/* 用户须知与并发限制（快速上手之前，醒目） */}
      <section className="gd-section gd-reveal">
        <NoticeCards />
      </section>

      {/* 快速上手向导 */}
      <section className="gd-section gd-reveal">
        <h2><Icon as={Rocket} size={20} /> 快速上手</h2>
        <QuickStartWizard />
      </section>

      {/* 功能教学卡片 */}
      <section className="gd-section">
        <h2><Icon as={Books} size={20} /> 功能说明</h2>
        <p className="gd-section-sub">点击卡片打开完整教学，每个功能都配有演示和可直接试用的示例问题。</p>

        {/* 分组标签（key 强制重建避免 reveal 状态残留导致切换后不可见） */}
        <div className="gd-tabs" role="tablist">
          {grouped.map(({ group }) => (
            <button
              key={group}
              role="tab"
              aria-selected={activeGroup === group}
              className={`gd-tab ${activeGroup === group ? 'gd-tab-active' : ''}`}
              onClick={() => setActiveGroup(group)}
            >
              {group}
            </button>
          ))}
        </div>

        <div className="gd-grid" key={activeGroup}>
          {grouped
            .find((g) => g.group === activeGroup)
            ?.items.map((f) => (
              <div key={f.id} className="gd-reveal">
                <FeatureCard feature={f} onOpen={setOpenFeature} disabled={f.id === 'ceo'} />
              </div>
            ))}
        </div>
      </section>

      {/* 常见问题 */}
      <section className="gd-section gd-reveal">
        <h2><Icon as={Question} size={20} /> 常见问题</h2>
        <FaqSection />
      </section>

      {/* 底部引导 */}
      <section className="gd-footer-cta">
        <p>还有不会的？去智能助手里直接问 AI，或点「反馈」告诉我们。</p>
        <button className="gd-btn gd-btn-primary gd-btn-lg" onClick={() => navigate('/qa')}>开始使用 →</button>
      </section>

      {/* 功能教学浮窗 */}
      {openFeature && (
        <div className="gd-modal-mask" onClick={() => setOpenFeature(null)}>
          <div className="gd-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
            <button className="gd-modal-close" aria-label="关闭" onClick={() => setOpenFeature(null)}>×</button>
            <div className="gd-modal-head">
              <div className="gd-card-icon"><Icon as={openFeature.icon} size={22} /></div>
              <div>
                <h3>{openFeature.title}</h3>
                <p>{openFeature.desc}</p>
              </div>
            </div>
            <div className="gd-modal-body">
              <div className="gd-sec-title"><Icon as={ListChecks} /> 怎么用</div>
              <ol className="gd-steps">
                {openFeature.steps.map((s, i) => <li key={i}>{s}</li>)}
              </ol>
              <div className="gd-sec-title"><Icon as={PlayCircle} /> 演示一下</div>
              <SimDemo feature={openFeature} onTryIt={tryIt} />
              <div className="gd-sec-title"><Icon as={Warning} /> 注意</div>
              <ul className="gd-tips">
                {openFeature.tips.map((t, i) => <li key={i}>{t}</li>)}
              </ul>
            </div>
            <div className="gd-modal-foot">
              <button className="gd-btn gd-btn-primary" onClick={() => tryIt(openFeature.example)}>
                去真实试一试 →
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
