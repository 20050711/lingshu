/**
 * 内嵌模拟对话演示：点击示例问题 → 逐步播放预置演示脚本。
 * 布局对齐真实使用（QA 页）：左侧对话流（用户/AI 气泡），右侧结果区（表格/图表）。
 * 不能骗用户——演示明确标注"模拟"，且输出形态与真实布局一致。
 */
import { useEffect, useRef, useState } from 'react'
import { ArrowDown, Check, DownloadSimple, FileText, PlayCircle } from '@phosphor-icons/react'
import Icon from '../../components/Icon'
import type { DemoStep, Feature } from './features'

interface Props {
  feature: Feature
  onTryIt: (question: string) => void
}

const TYPE_SPEED = 26        // 打字机速度（ms/字符）——2026-08-11 用户要求放慢
const STEP_DELAY = 800       // 步骤间停顿
const TOOL_DELAY = 1400      // 工具调用动画时长

function ToolBadge({ name }: { name: string }) {
  return (
    <div className="gd-demo-tool">
      <span className="gd-demo-tool-dot" />
      <span>{name}</span>
      <span className="gd-demo-tool-dots">
        <i /><i /><i />
      </span>
    </div>
  )
}

function MiniChart({ title, labels, values }: { title: string; labels: string[]; values: number[] }) {
  const max = Math.max(...values, 1)
  return (
    <div className="gd-demo-chart">
      <div className="gd-demo-chart-title">{title}</div>
      <div className="gd-demo-chart-bars">
        {labels.map((l, i) => (
          <div key={l} className="gd-demo-chart-col">
            <div className="gd-demo-chart-bar" style={{ height: `${Math.round((values[i] / max) * 100)}%` }} />
            <span className="gd-demo-chart-label">{l}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function SimDemo({ feature, onTryIt }: Props) {
  const [playing, setPlaying] = useState(false)
  const [stepIdx, setStepIdx] = useState(0)
  const [typedLen, setTypedLen] = useState(0)
  // 最后的结果步骤（表格/图表/文档/图片/视频）：播放结束后持续显示，再次点击演示才清除
  const [lastResult, setLastResult] = useState<DemoStep | null>(null)
  const timers = useRef<number[]>([])

  const clearTimers = () => {
    timers.current.forEach(clearTimeout)
    timers.current = []
  }

  useEffect(() => () => clearTimers(), [])

  const reset = () => {
    clearTimers()
    setPlaying(false)
    setStepIdx(0)
    setTypedLen(0)
    setLastResult(null)
  }

  const play = () => {
    reset()
    setPlaying(true)
    const steps = feature.demo

    const runStep = (i: number) => {
      if (i >= steps.length) {
        setPlaying(false)
        return
      }
      const s = steps[i]
      setStepIdx(i)
      setTypedLen(0)
      // 结果类步骤记录为 lastResult（播放结束后持久显示）
      if (s.type === 'table' || s.type === 'chart' || s.type === 'doc' || s.type === 'image' || s.type === 'video') {
        setLastResult(s)
      }

      if (s.type === 'text') {
        const total = s.content.length
        const iv = window.setInterval(() => {
          setTypedLen((n) => {
            if (n + 1 >= total) {
              clearInterval(iv)
              timers.current.push(window.setTimeout(() => runStep(i + 1), STEP_DELAY))
              return total
            }
            return n + 1
          })
        }, TYPE_SPEED)
        timers.current.push(iv as unknown as number)
      } else if (s.type === 'tool') {
        timers.current.push(window.setTimeout(() => runStep(i + 1), TOOL_DELAY))
      } else if (s.type === 'done') {
        timers.current.push(window.setTimeout(() => setPlaying(false), 600))
      } else {
        timers.current.push(window.setTimeout(() => runStep(i + 1), STEP_DELAY + 350))
      }
    }
    runStep(0)
  }

  const step = feature.demo[stepIdx]
  // 结果类步骤（表格/图表/文档/图片/视频）显示在右侧结果区；播放结束后 lastResult 持久显示
  const isResult = step?.type === 'table' || step?.type === 'chart' || step?.type === 'doc' || step?.type === 'image' || step?.type === 'video'
  const shownResult = playing && isResult ? step : lastResult
  const isStepFlow = feature.demo.some((s) => s.type === 'step')
  const finished = !playing && stepIdx > 0

  return (
    <div className="gd-demo">
      {isStepFlow ? (
        /* 工具类功能：操作步骤流程（非对话型，用对应工具的使用方式演示） */
        <div className="gd-demo-steps">
          {feature.demo.filter((s) => s.type === 'step').map((s, i) => (
            <div key={i} className={`gd-demo-step ${playing && stepIdx === i ? 'gd-step-active' : playing && stepIdx > i ? 'gd-step-done' : ''}`}>
              <div className="gd-demo-step-no">{playing && stepIdx > i ? <Icon as={Check} size={13} /> : i + 1}</div>
              <div className="gd-demo-step-body">
                <div className="gd-demo-step-title">{s.type === 'step' ? s.title : ''}</div>
                <div className="gd-demo-step-desc">{s.type === 'step' ? s.desc : ''}</div>
              </div>
            </div>
          ))}
        </div>
      ) : (
      /* 对话型功能：模拟真实布局的"对话 / 结果区"双栏 */
      <div className="gd-demo-layout">
        {/* 左侧：对话流 */}
        <div className="gd-demo-col gd-demo-col-left">
          <div className="gd-demo-col-label">对话</div>
          <div className="gd-demo-convo">
            <div className="gd-demo-user">
              <span className="gd-demo-avatar gd-avatar-user">我</span>
              <span className="gd-demo-user-bubble">{feature.example}</span>
            </div>
            {playing && step && !isResult && (
              <div className="gd-demo-ai">
                <span className="gd-demo-avatar gd-avatar-ai">AI</span>
                <div className="gd-demo-ai-body">
                  {step.type === 'text' && (
                    <div className="gd-demo-text">
                      {step.content.slice(0, typedLen)}
                      <span className="gd-demo-caret" />
                    </div>
                  )}
                  {step.type === 'tool' && <ToolBadge name={step.name} />}
                  {step.type === 'done' && <div className="gd-demo-done"><Icon as={Check} size={14} /> 回答完成</div>}
                </div>
              </div>
            )}
            {finished && (
              <div className="gd-demo-ai">
                <span className="gd-demo-avatar gd-avatar-ai">AI</span>
                <div className="gd-demo-ai-body">
                  <div className="gd-demo-text">回答完成（结果见右侧结果区）</div>
                </div>
              </div>
            )}
            {!playing && stepIdx === 0 && (
              <div className="gd-demo-hint"><Icon as={ArrowDown} /> 点下方「开始演示」，看看 AI 会怎么回答</div>
            )}
          </div>
        </div>

        {/* 右侧：结果区（表格/图表，对齐真实预览区形态） */}
        <div className="gd-demo-col gd-demo-col-right">
          <div className="gd-demo-col-label">结果区</div>
          <div className="gd-demo-result">
            {shownResult ? (
              <>
                {shownResult.type === 'table' && (
                  <table className="gd-demo-table">
                    <thead>
                      <tr>{shownResult.columns.map((c) => <th key={c}>{c}</th>)}</tr>
                    </thead>
                    <tbody>
                      {shownResult.rows.map((r, ri) => (
                        <tr key={ri}>{r.map((v, vi) => <td key={vi}>{v}</td>)}</tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {shownResult.type === 'chart' && (
                  <MiniChart title={shownResult.title} labels={shownResult.labels} values={shownResult.values} />
                )}
                {shownResult.type === 'doc' && (
                  <div className="file-card">
                    <span className="file-card-icon"><Icon as={FileText} size={24} /></span>
                    <div className="file-card-body">
                      <div className="file-card-name">{shownResult.name}</div>
                    </div>
                    <div className="file-card-actions">
                      <a className="file-card-btn" href={shownResult.file} download>
                        <Icon as={DownloadSimple} size={13} /> 下载示例
                      </a>
                    </div>
                  </div>
                )}
                {shownResult.type === 'image' && (
                  <img className="gd-demo-image" src={shownResult.file} alt="示例图片" />
                )}
                {shownResult.type === 'video' && (
                  <video className="gd-demo-video" src={shownResult.file} controls muted />
                )}
              </>
            ) : (
              <div className="gd-demo-result-empty">
                {finished ? '结果已展示在右侧结果区（保持显示，可再次点击演示重新播放）' : '结果会显示在这里（和真实使用一样）'}
              </div>
            )}
          </div>
        </div>
      </div>
      )}

      {/* 控制区 */}
      <div className="gd-demo-actions">
        <button className="gd-btn gd-btn-primary" onClick={playing ? reset : play} disabled={playing && !stepIdx}>
          {playing ? '停止演示' : stepIdx > 0 ? '再看一遍' : (<><Icon as={PlayCircle} /> 开始演示</>)}
        </button>
        <button className="gd-btn gd-btn-ghost" onClick={() => onTryIt(feature.example)}>
          去真实试一试 →
        </button>
        <span className="gd-demo-note">实际使用时结果会出现在右侧结果区</span>
      </div>
    </div>
  )
}
