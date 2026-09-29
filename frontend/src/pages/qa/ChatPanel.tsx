// 对话区（v2 交互框架）：消息流 + 双模式按钮 + 反问卡/批准卡/todo 清单 + 常开输入框 + 浮窗面板
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { API_PREFIX } from '../../api/prefix'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Modal, Popconfirm, Select, message } from 'antd'
import { ArrowsInLineVertical, Check, CircleNotch, ClipboardText, Compass, Lightning, Robot, Warning, X } from '@phosphor-icons/react'
import Icon, { iconFromKey } from '../../components/Icon'
import DraggableModal from '../../components/DraggableModal'
import UploadProgress from '../../components/UploadProgress'
import { errMessage } from '../../api/client'
import { useQAStore } from '../../stores/qaStore'
import { useAuthStore } from '../../stores/authStore'
import { estimateCost } from '../../lib/costEstimate'  // 需求 4：token 费用估算

// 禁删除线渲染：模型偶发输出 ~~…~~（DeepSeek 习惯），预处理为普通文本；
// 单波浪 ~text~ 的删除线由 remarkGfm({singleTilde:false}) 禁用解析（2026-08-18：
// LLM 输出「（~癸），数值为 25~）」「甲~癸」被单波浪删除线误伤——渲染器层面关闭，
// 比正则猜测更可靠，且不误伤「25~96」「37~88」等多范围波浪）
function stripStrikethrough(text: string) {
  return text.replace(/~~/g, '')
}

// 剥离动态上下文块（四期缓存优化：记忆/文件/运行状态上下文随 user 消息注入模型，但不对用户展示；
// 2026-08-18：/g 全局匹配——运行状态注记也包进上下文标记块（agent_llm 注入））
function stripCtx(text: string) {
  return text.replace(/【上下文开始】[\s\S]*?【上下文结束】\n*/g, '')
}

// 剥离 LLM 幻觉输出的工具调用 XML（2026-08-07：后端已在节点剥离，此处兜底历史脏数据/流式残余）
function stripToolXml(text: string) {
  return text
    .replace(/<tool_calls>[\s\S]*?<\/tool_calls>/g, '')
    .replace(/<invoke name=[\s\S]*?<\/invoke>/g, '')
    .replace(/<parameter name=[\s\S]*?<\/parameter>/g, '')
    .replace(/<\/?(?:invoke|parameter|tool_calls|tool_call|search_query)[^>]*>/gi, '')
}

// 2026-09-20（走查"完整的输出还是 md 文本、过一会才变表格"）：流式期间**按 markdown 渲染（节流 300ms）**。
// 背景：原实现流式期间显示纯文本，而"切成 markdown"的开关绑在**轮次结束**事件上——后端收尾还要跑一次
// verify 校验 + 落库（数秒），于是用户看到"内容已经完整、却仍是 md 原文"。
// 节流后：答案一吐完表格立刻成形；代价只是每 300ms 一次解析（表格大时可忽略）。
function StreamMarkdown({ text }: { text: string }) {
  const [shown, setShown] = useState(text)
  const lastRef = useRef(0)
  useEffect(() => {
    const now = Date.now()
    const wait = Math.max(0, 300 - (now - lastRef.current))
    if (wait === 0) {
      lastRef.current = now
      setShown(text)
      return
    }
    const t = setTimeout(() => { lastRef.current = Date.now(); setShown(text) }, wait)
    return () => clearTimeout(t)
  }, [text])
  return (
    <ReactMarkdown remarkPlugins={[[remarkGfm, { singleTilde: false }]]}>
      {stripStrikethrough(shown)}
    </ReactMarkdown>
  )
}

/* ===== v2 工具明细：中文显示名叙述行（不暴露原始工具名） ===== */
// 静态兜底映射（/api/v1/skills 拉取后优先用后端 display_name；网络失败用此表）
const TOOL_LABEL_FALLBACK: Record<string, string> = {
  generate_chart: '图表生成', doc_export: '文档产出', html_report: '网页报告',
  file_parse: '文件解析', web_search: '联网搜索', kb_match: '知识库检索', kb_read: '知识库全文阅读',
  read_output: '产出读取', run_script: '沙盒脚本', subagent: '子代理', image_generation: '图片生成',
  image_recognition: '图片识别', video_generate: '视频生成', memory: '记忆库',
  intent_event: '事件广播', result_event: '事件广播', todo_step: '步骤清单',
  // 2026-08-31：补全会话级工具（懒加载失败时兜底中文，防显示英文工具名）
  zip_extract: '解压 zip', zip_pack: '打包 zip', ask_user: '提问确认', skill_read: '技能说明',
}
let toolLabelCache: Record<string, string> | null = null
function toolLabel(name: string): string {
  if (!toolLabelCache) {
    // 惰性拉取一次（失败留空走 fallback）
    fetch(`${API_PREFIX}/skills`).then((r) => r.ok ? r.json() : Promise.reject()).then((d) => {
      const map: Record<string, string> = {}
      for (const t of (d.tools || [])) if (t?.id && t?.name) map[t.id] = t.name
      toolLabelCache = map
    }).catch(() => {})
    toolLabelCache = {}
  }
  return toolLabelCache[name] || TOOL_LABEL_FALLBACK[name] || name
}

/* 2026-09-18 走查：时间线行首的状态标记是 **SVG**（Phosphor），而原实现整行用
   alignItems:'baseline' 对齐——SVG 没有基线，浏览器拿它的底边当基线，于是 ✗/✓ 被整体顶高、
   和 13px 文字对不齐（用户报「工具调用记录前面的 ✗ 布局有问题」）。
   改为：行首标记包一个「一个行高」的居中盒子（1.8em = 行 lineHeight），行容器改 flex-start——
   首行内图标垂直居中，正文换行时标记也不会掉到中间。 */
const MARK_BOX: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  height: '1.8em',
  flexShrink: 0,
}

/* ===== 2026-08-18：事件时间线（预告→工具→小结 按到达顺序平铺循环，13px） ===== */
function TimelineList({ timeline }: { timeline: any[] }) {
  if (!timeline?.length) return null
  return (
    <div style={{ marginBottom: 6, maxWidth: '100%', display: 'flex', flexDirection: 'column', gap: 2 }}>
      {timeline.map((t: any, i: number) => {
        // 2026-08-20（走查修正）：插话条目——插在时间线当前进度点（紧跟正在执行的步骤），
        // 右侧对齐用户样式 + 「已插入」小注（后续工具事件继续追加其后，标记插入时刻位置）
        if (t.kind === 'interrupt') {
          return (
            <div key={i} style={{ fontSize: 13, color: 'var(--primary)', lineHeight: 1.8, display: 'flex', alignItems: 'baseline', gap: 4, justifyContent: 'flex-end' }}>
              <span style={{ minWidth: 0 }}>{t.text}</span>
              <span style={{ fontSize: 11, color: 'var(--text-3)', whiteSpace: 'nowrap' }}>已插入计划，当前步骤完成后生效</span>
            </div>
          )
        }
        if (t.kind === 'intent') {
          return (
            <div key={i} data-testid="tl-intent" style={{ fontSize: 13, color: 'var(--brand-ink)', lineHeight: 1.8, display: 'flex', alignItems: 'baseline', gap: 4 }}>
              <span style={{ fontSize: 11 }}>▶</span>
              <span style={{ minWidth: 0 }}>{t.text}</span>
            </div>
          )
        }
        if (t.kind === 'result') {
          return (
            <div key={i} data-testid="tl-result" style={{ fontSize: 13, color: t.ok ? 'var(--success)' : '#dc2626', lineHeight: 1.8, display: 'flex', alignItems: 'flex-start', gap: 4 }}>
              <span style={MARK_BOX}>{t.ok ? <Icon as={Check} size={13} /> : <Icon as={X} size={13} />}</span>
              <span style={{ minWidth: 0, flex: 1 }}>{t.text}</span>
              {t.duration_s != null && <span style={{ color: 'var(--text-3)', fontSize: 12, whiteSpace: 'nowrap' }}>{t.duration_s}s</span>}
            </div>
          )
        }
        // 工具行（含 subagent 徽标；同轮 start/done 合并为一行）
        const isSubagent = t.tool_name === 'subagent'
        return (
          <div key={i}>
            <div data-testid="tl-tool" style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.8, display: 'flex', alignItems: 'flex-start', gap: 4, paddingLeft: t.subagent_id ? 14 : 0 }}
              title={t.detail || ''}>
              <span style={{ ...MARK_BOX, color: t.status === 'error' ? '#dc2626' : t.status === 'done' ? 'var(--success)' : 'var(--text-3)' }}>
                {t.status === 'done' ? <Icon as={Check} size={13} /> : t.status === 'error' ? '!' : '…'}
              </span>
              {t.subagent_id && (
                <span style={{ color: 'var(--brand-ink)', background: 'var(--brand-soft)', borderRadius: 4, padding: '0 4px', whiteSpace: 'nowrap', fontSize: 11 }}>
                  {t.subagent_id}
                </span>
              )}
              <b style={{ color: 'var(--text-1)', whiteSpace: 'nowrap' }}>{t.label || toolLabel(t.tool_name)}</b>
              {(t.brief || t.detail) ? (
                <span style={{ color: 'var(--text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, flex: 1 }}>
                  — {t.brief || t.detail}
                </span>
              ) : null}
            </div>
            {/* 2026-08-18：子代理完整回复直接展示；2026-08-20（滚动卡顿修复③）：加 240px 上限
                可滚动查看——原不截断导致长会话 DOM/绘制成本线性膨胀（内容仍完整，滚动查看） */}
            {t.output ? (
              <pre style={{
                margin: '2px 0 6px 14px', fontSize: 12, color: 'var(--text-2)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                maxHeight: isSubagent ? 240 : 120, overflow: 'auto', background: 'var(--surface-inset)', borderRadius: 6, padding: '6px 10px',
              }}>
                {t.output}
              </pre>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

/* ===== think 标签（2026-08-18：小号底色标签 + 旋转弧环，无气泡框；仅纯思考时显示） ===== */
function ThinkTag() {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 6,
      background: 'var(--brand-soft)', color: 'var(--brand-ink)', borderRadius: 6, padding: '2px 10px', fontSize: 12,
    }}>
      思考中
      <Icon as={CircleNotch} size={12} className="icon-spin" />
    </span>
  )
}

const MessageItem = memo(function MessageItem({ m, onJump }: { m: any; onJump: (roundId?: number) => void }) {
  const isUser = m.role === 'user'
  const files = m.files || []
  // 4.1 根因①：非流式且内容为空（纯工具轮）→ 不渲染空气泡
  const finalText = m.streaming ? '' : stripStrikethrough(stripToolXml(stripCtx(m.content || ''))).trim()
  // 4.1 根因③：流式渲染前去两端换行（pre-wrap 下不再撑出空白区）
  const streamText = stripToolXml(stripCtx(m.content || '')).replace(/^\n+|\n+$/g, '')
  // 2026-08-18：有进行中的工具 → 不显示 think 标签（工具行本身就是过程展示）
  const hasRunningTool = (m.timeline || []).some((t: any) => t.kind === 'tool' && t.status !== 'done' && t.status !== 'error')
  const showBubble = m.streaming ? streamText.length > 0 : finalText.length > 0
  // 2026-09-08：平台说明（强制收尾两分类模板）——灰色底 + 系统标签，与 LLM 消息视觉区分
  const isPlatform = !isUser && (finalText.startsWith('【平台说明】') || streamText.startsWith('【平台说明】'))
  return (
    <div style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start', marginBottom: 14 }}>
      <div style={{ maxWidth: '78%', minWidth: 0 }}>
        {/* 2026-08-18：事件时间线平铺（预告→工具→小结循环），替代原收缩明细块/意图气泡/结果条 */}
        {!isUser && <TimelineList timeline={m.timeline} />}
        {isUser && files.length > 0 && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 6, justifyContent: 'flex-end' }}>
            {files.map((f: any, i: number) => (
              <span key={i} className="badge badge-blue">文件：{f.file_name}</span>
            ))}
          </div>
        )}
        {showBubble && (
          <div
            className="msg-bubble"
            style={{
              background: isPlatform ? 'var(--surface-2)' : isUser ? (m.interrupt ? 'var(--brand-soft)' : 'var(--brand)') : 'var(--surface-2)',
              color: isPlatform ? 'var(--text-1)' : isUser ? (m.interrupt ? 'var(--brand-ink)' : '#fff') : 'var(--text-1)',
              border: isPlatform ? '1px solid var(--hairline)' : isUser ? (m.interrupt ? '1px dashed var(--brand)' : 'none') : '1px solid var(--hairline)',
            }}
          >
            {isPlatform && (
              <div style={{ fontSize: 11, color: 'var(--text-2)', marginBottom: 4 }}>系统说明（非 AI 回复）</div>
            )}
            {m.streaming ? (
              // 流式也渲染 markdown（节流）；表格不再等"轮次结束"才成形
              <StreamMarkdown text={streamText} />
            ) : isUser ? (
              // C29（2026-08-12）：用户消息原文展示——原走 markdown 把 `__text__` 转成加粗，原文非原样
              <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{finalText}</span>
            ) : (
              // 2026-08-18：assistant 渲染前剥离双波浪删除线 + 禁用单波浪删除线解析
              // （remark-gfm singleTilde:false——LLM 输出「~癸）~」等单波浪不再渲染删除线）
              <ReactMarkdown remarkPlugins={[[remarkGfm, { singleTilde: false }]]}>{stripStrikethrough(finalText)}</ReactMarkdown>
            )}
          </div>
        )}
        {/* 2026-08-18：纯思考态（流式中无文本且无进行中工具）→ think 小标签 + 旋转弧环，无气泡框 */}
        {!isUser && m.streaming && !streamText && !hasRunningTool && <ThinkTag />}
        {/* v2 插话气泡标注 */}
        {m.interrupt && m.sysHint && (
          <div style={{ marginTop: 4, fontSize: 11, color: 'var(--primary)', textAlign: 'right' }}>{m.sysHint}</div>
        )}
        {m.sysHint && !isUser && !m.interrupt && (
          <div
            onClick={() => onJump(m.round_id)}
            style={{
              marginTop: 6, display: 'inline-block', padding: '4px 10px', borderRadius: 999,
              background: 'var(--primary-bg)', color: 'var(--primary)', fontSize: 12, cursor: 'pointer',
            }}
          >
            {m.sysHint}
          </div>
        )}
        {/* 4.1：回复总耗时（done 事件携带，无 emoji） */}
        {!isUser && !m.streaming && m.elapsedS != null && (
          <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-3)' }}>耗时 {m.elapsedS} 秒</div>
        )}
      </div>
    </div>
  )
})

/* ===== v2 双模式按钮（⚡快速 / 🧭复杂；仅允许升级） ===== */
function ModeSwitch() {
  const mode = useQAStore((s) => s.mode)
  const streaming = useQAStore((s) => s.streaming)
  const messages = useQAStore((s) => s.messages)
  const setMode = useQAStore((s) => s.setMode)
  const upgradeToComplex = useQAStore((s) => s.upgradeToComplex)
  const pickComplex = () => {
    // K3（2026-08-19）：store streaming 与消息级 streaming 标志可能短暂不同步
    // （done 帧处理顺序）——以最后一条消息的真实流式状态为准，避免偶发吞掉升级 Modal
    const lastMsg = messages[messages.length - 1]
    const effectivelyStreaming = streaming && lastMsg?.role === 'assistant' && !!lastMsg.streaming
    if (effectivelyStreaming) { message.info('正在生成中，本轮结束后再切换'); return }
    // v2 用户反馈（bug 级）：空对话/无历史时点 🧭 只是**选择模式**，不应直接发升级消息；
    // 有对话历史才走 P0 升级（基于全部对话重新规划）
    if (messages.length) {
      Modal.confirm({
        title: '升级为复杂任务模式',
        content: '将基于当前对话内容重新规划，继续？',
        okText: '升级',
        cancelText: '取消',
        // 2026-08-19（S1-2 根因）：升级是后台任务（task_bg_enabled 下 agent 独立于请求
        // 生命周期），onOk 必须**不返回** upgradeToComplex 的 Promise——否则 antd 在
        // Promise resolve（=升级轮整个流式 done）前保持 Modal 打开，全屏遮罩拦截
        // 反问卡/批准卡点击（曾致「无 /chat/answer + 300s 批准卡超时」）。
        onOk: () => {
          upgradeToComplex()  // fire-and-forget：点击即关 Modal，升级后台继续
        },
      })
      return
    }
    setMode('complex')  // 空态：仅切换模式，下一条消息按 complex 发送
  }
  // 2026-08-14 反馈：新对话（无任何消息）应允许快速/复杂**双向自由切换**——
  // 原 quick 按钮在 complex 下恒禁用（"不支持降级"语义误伤空对话）；有历史后才锁定升级语义
  const empty = !messages.length
  return (
    <div className="mode-switch" style={{ display: 'inline-flex', gap: 4 }}>
      <button className={`mode-btn ${mode === 'quick' ? 'active' : ''}`}
        disabled={streaming || (mode === 'complex' && !empty)}
        title={mode === 'complex' && !empty ? '复杂任务模式不支持降级' : '快速任务'}
        onClick={() => { if (mode === 'complex') setMode('quick') }}><Icon as={Lightning} /> 快速任务</button>
      <button className={`mode-btn ${mode === 'complex' ? 'active' : ''}`} disabled={streaming}
        title="复杂任务（完整流程：澄清→调研→设计→批准→执行）" onClick={() => { if (mode === 'quick') pickComplex() }}><Icon as={Compass} /> 复杂任务</button>
    </div>
  )
}

/* ===== v2 反问浮窗卡（输入框上方；120s 倒计时，超时按推荐项自动提交） ===== */
function QuestionCard() {
  const questionState = useQAStore((s) => s.questionState)
  const answerQuestion = useQAStore((s) => s.answerQuestion)
  const dismissQuestion = useQAStore((s) => s.dismissQuestion)
  const [answers, setAnswers] = useState<Record<number, { selected: number[]; other: string }>>({})
  const [extra, setExtra] = useState('')
  const [busy, setBusy] = useState(false)
  const [collapsed, setCollapsed] = useState(false)  // 2026-08-18：提问卡可收纳
  const [remain, setRemain] = useState<number | null>(null)
  const submittedRef = useRef(false)
  useEffect(() => {
    if (!questionState) { setAnswers({}); setExtra(''); submittedRef.current = false; setBusy(false); return }
    const init: Record<number, any> = {}
    questionState.questions.forEach((q, i) => { init[i] = { selected: [...(q.recommended || [0])], other: '' } })
    setAnswers(init)
    setExtra('')
    submittedRef.current = false
  }, [questionState?.question_id])
  // 倒计时：到点后端已按推荐项自动提交，前端清卡
  useEffect(() => {
    if (!questionState) { setRemain(null); return }
    const tick = () => {
      const left = Math.max(0, Math.ceil((questionState.deadline - Date.now()) / 1000))
      setRemain(left)
      if (left <= 0) { dismissQuestion(); clearInterval(t) }
    }
    tick()
    const t = setInterval(tick, 1000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questionState?.question_id])
  if (!questionState) return null
  // 2026-08-18：提交加前端超时兜底（axios 60s 超时在极端场景仍可能长时间停留"提交中"）
  const doSubmit = (answers: { index: number; selected: number[]; other_text: string }[], txt: string) => {
    if (busy || submittedRef.current) return
    submittedRef.current = true
    setBusy(true)
    const timer = setTimeout(() => {
      submittedRef.current = false
      setBusy(false)
      message.warning('提交超时，请重试（若对话已继续可忽略）')
    }, 15000)
    answerQuestion(answers, txt)
      // 2026-08-19（S1-2）：成功路径必须复位 busy——原只 catch/超时复位，成功后 busy
      // 永 true → 下一张反问卡按钮 disabled（多轮反问场景"全部默认"点击失效）
      .then(() => setBusy(false))
      .catch((e: any) => { message.warning(e?.message || '回答已过期'); setBusy(false) })
      .finally(() => clearTimeout(timer))
  }
  const submit = () => {
    // 2026-08-31：单选模式选了「其他」但没填内容 → 提示（避免提交空"自定义"）
    for (const [i, q] of questionState.questions.entries()) {
      const cur = answers[i] || { selected: [], other: '' }
      if (!q.multi_select && cur.selected.length === 0 && !(cur.other || '').trim()) {
        message.warning(`请填写 Q${i + 1} 的内容（选了"其他"需输入）或选择选项`)
        return
      }
    }
    doSubmit(questionState.questions.map((_, i) => ({
      index: i, selected: answers[i]?.selected ?? [], other_text: answers[i]?.other ?? '',
    })), extra)
  }
  const submitDefaults = () => {
    doSubmit(questionState.questions.map((q, i) => ({
      index: i, selected: [...(q.recommended || [0])], other_text: '',
    })), extra)
  }
  return (
    <div className="card question-card">
      <div style={{ fontWeight: 600, marginBottom: collapsed ? 0 : 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer', userSelect: 'none' }}
        onClick={() => setCollapsed((v) => !v)}>
        <span>开始前我需要确认几件事</span>
        <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{collapsed ? '展开 ▾' : '收起 ▴'}
          {remain != null && !collapsed && <span style={{ color: 'var(--warning)', marginLeft: 8 }}>剩余 {remain}s</span>}
        </span>
      </div>
      {collapsed ? null : (
      <>
      {questionState.questions.map((q, qi) => {
        const cur = answers[qi] || { selected: [], other: '' }
        return (
          <div key={qi} style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 6 }}>Q{qi + 1} {q.text}</div>
            {q.options.map((opt, oi) => (
              <label key={oi} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-1)', padding: '3px 0', cursor: 'pointer' }}>
                <input type={q.multi_select ? 'checkbox' : 'radio'} name={`q${qi}`}
                  checked={cur.selected.includes(oi)}
                  onChange={() => {
                    let sel: number[]
                    if (q.multi_select) sel = cur.selected.includes(oi) ? cur.selected.filter((x) => x !== oi) : [...cur.selected, oi]
                    else sel = [oi]
                    setAnswers((a) => ({ ...a, [qi]: { ...cur, selected: sel } }))
                  }} />
                {opt}
                {(q.recommended || []).includes(oi) && <span className="badge badge-blue" style={{ fontSize: 10 }}>推荐</span>}
              </label>
            ))}
            {/* 2026-08-31：单选模式加「其他（自定义输入）」——不选已有选项，直接输入自己的答案 */}
            {!q.multi_select && (
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--brand-ink)', padding: '3px 0', cursor: 'pointer' }}>
                <input type="radio" name={`q${qi}`}
                  checked={cur.selected.length === 0}
                  onChange={() => {
                    setAnswers((a) => ({ ...a, [qi]: { ...cur, selected: [] } }))
                    const el = document.getElementById(`q-other-${qi}`)
                    if (el) setTimeout(() => el.focus(), 0)
                  }} />
                其他（自定义输入）
              </label>
            )}
            <input id={`q-other-${qi}`} className="chat-input" style={{ marginTop: 4, fontSize: 12, height: 28 }}
              placeholder={q.multi_select ? 'Other：其他补充（可不填）' : '选择「其他」后在此输入你的答案'} value={cur.other}
              onChange={(e) => setAnswers((a) => ({ ...a, [qi]: { ...cur, other: e.target.value } }))} />
          </div>
        )
      })}
      <input className="chat-input" style={{ fontSize: 12, height: 28, marginBottom: 8 }}
        placeholder="补充说明（可选，随回答一并提交）" value={extra} onChange={(e) => setExtra(e.target.value)} />
      <div style={{ display: 'flex', gap: 8 }}>
        <button className="btn-primary" disabled={busy} onClick={submit}>{busy ? '提交中…' : '提交回答'}</button>
        <button className="btn-ghost" disabled={busy} onClick={submitDefaults}>全部默认 = 按推荐项</button>
      </div>
      </>
      )}
    </div>
  )
}

/* ===== v2 计划批准卡（唯一人工门；DraggableModal 居中浮窗） ===== */
function PlanApprovalCard() {
  const planCard = useQAStore((s) => s.planCard)
  const approvePlan = useQAStore((s) => s.approvePlan)
  const [busy, setBusy] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [collapsed, setCollapsed] = useState(false)  // 2026-08-18：计划卡可收纳
  const [feedback, setFeedback] = useState('')
  const [remain, setRemain] = useState<number | null>(null)
  // 2026-08-19（S1-2）：plan_id 变化重置 rejecting/busy/feedback——React 对同位置组件
  // return null 保留实例 state（reject 后 planCard=null 期间 rejecting 保持 true），
  // 修订重提新卡渲染时曾显示「提交不同意」而非「同意执行」，用户无法批准。
  useEffect(() => { setRejecting(false); setBusy(false); setFeedback('') }, [planCard?.plan_id])
  useEffect(() => {
    if (!planCard || planCard.status !== 'pending' || !planCard.expires_at) { setRemain(null); return }
    const tick = () => {
      const left = Math.max(0, Math.ceil((new Date(planCard.expires_at!).getTime() - Date.now()) / 1000))
      setRemain(left)
      if (left <= 0) clearInterval(t)
    }
    tick()
    const t = setInterval(tick, 1000)
    return () => clearInterval(t)
  }, [planCard?.plan_id])
  if (!planCard) return null
  if (planCard.status !== 'pending') return null  // confirmed 由 TodoList 承接；readonly 由提示条承接
  const act = (decision: 'approve' | 'reject') => {
    if (busy) return
    if (decision === 'reject' && !rejecting) { setRejecting(true); return }
    setBusy(true)
    approvePlan(decision, decision === 'reject' ? feedback.trim() : '')
      .catch((e: any) => { message.warning(e?.message || '批准已过期'); setBusy(false) })
  }
  return (
    <DraggableModal
      open
      closable={false}
      centered
      footer={null}
      width={560}
      title={
        <span style={{ cursor: 'pointer', userSelect: 'none' }} onClick={() => setCollapsed((v) => !v)}>
          <Icon as={ClipboardText} /> 计划批准{planCard.revision > 1 ? `（第 ${planCard.revision} 版）` : ''}
          <span style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 8 }}>{collapsed ? '展开 ▾' : '收起 ▴'}</span>
        </span>
      }
    >
      {!collapsed && (<>
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 6 }}>{planCard.goal}</div>
        {remain != null && <div style={{ fontSize: 11, color: 'var(--warning)' }}>剩余 {remain}s 未操作将暂停本轮（发送任意消息可重新打开）</div>}
      </div>
      <div style={{ maxHeight: 320, overflow: 'auto', marginBottom: 10 }}>
        <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 4 }}>步骤（{planCard.steps.length} 步）</div>
        {planCard.steps.map((s: any, i: number) => (
          <div key={s.id} style={{ padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 8, marginBottom: 6 }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>{i + 1}. {s.title}</div>
            <div style={{ fontSize: 12, color: 'var(--text-2)' }}>{s.intent}</div>
            <div style={{ fontSize: 11, color: 'var(--text-3)' }}>验证标准：{s.verify}</div>
          </div>
        ))}
        {planCard.risks.length > 0 && (
          <div style={{ fontSize: 12, color: 'var(--warning)', background: 'var(--warning-soft)', borderRadius: 6, padding: '8px 10px' }}>
            <Icon as={Warning} /> 风险：{planCard.risks.join('；')}
          </div>
        )}
      </div>
      {/* 2026-08-14 反馈：输入框常显——不同意必须有理由，点击「不同意」进入提交流程 */}
      <textarea className="chat-input" style={{ marginBottom: 8 }} rows={2} autoFocus={rejecting}
        placeholder="不同意原因（必填）：请说明需要如何调整" value={feedback}
        onChange={(e) => setFeedback(e.target.value)} />
      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        {rejecting && (
          <>
            <button className="btn-ghost" disabled={busy} onClick={() => { setRejecting(false); setFeedback('') }}>返回</button>
            <button className="btn-ghost" disabled={busy || !feedback.trim()} onClick={() => act('reject')}>提交不同意</button>
          </>
        )}
        {!rejecting && (
          <>
            <button className="btn-ghost" disabled={busy} onClick={() => act('reject')}><Icon as={X} /> 不同意</button>
            <button className="btn-primary" disabled={busy} onClick={() => act('approve')}>{busy ? '提交中…' : <><Icon as={Check} /> 同意执行</>}</button>
          </>
        )}
      </div>
      </>)}
    </DraggableModal>
  )
}

/* ===== v2 todo 步骤清单（批准后实时勾选；2026-08-14 可收缩） ===== */
function TodoList() {
  const planCard = useQAStore((s) => s.planCard)
  const todoState = useQAStore((s) => s.todoState)
  const lastStepIndex = useQAStore((s) => s.lastStepIndex)
  const [collapsed, setCollapsed] = useState(false)
  if (!planCard || planCard.status !== 'confirmed' || !planCard.steps.length) return null
  const doneCount = planCard.steps.filter((s: any) => todoState[s.id] === 'done').length
  const running = planCard.steps.findIndex((s: any) => !todoState[s.id]) + 1
  return (
    <div className="card todo-list" style={{ marginBottom: 10, padding: '10px 12px' }}>
      <div style={{ fontWeight: 600, fontSize: 12, color: 'var(--text-2)', display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer', userSelect: 'none' }}
        onClick={() => setCollapsed((v) => !v)}>
        <span>执行清单（{doneCount}/{planCard.steps.length} 步{!collapsed && running > 0 && doneCount < planCard.steps.length ? `，正在第 ${running} 步` : ''}）</span>
        <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{collapsed ? '展开 ▾' : '收起 ▴'}</span>
      </div>
      {!collapsed && planCard.steps.map((s: any, i: number) => {
        const st = todoState[s.id]
        const inProgress = !st && i + 1 === lastStepIndex
        return (
          <div key={s.id} className="todo-item" data-status={st || (inProgress ? 'running' : 'pending')}>
            <span className="todo-check">
              {st === 'done' ? <Icon as={Check} size={12} /> : st === 'failed' ? <Icon as={X} size={12} /> : inProgress ? '…' : ''}
            </span>
            <span className="todo-title" style={{ textDecoration: st === 'done' ? 'line-through' : 'none' }}>{s.title}</span>
            {st === 'failed' && <span style={{ fontSize: 11, color: 'var(--error)' }}>失败</span>}
          </div>
        )
      })}
    </div>
  )
}

/* ===== v2 子代理进度徽标 ===== */
function ProgressBadges() {
  const progressAgents = useQAStore((s) => s.progressAgents)
  const running = progressAgents.filter((p) => p.status === 'running')
  if (!running.length) return null
  return (
    <div style={{ marginBottom: 8, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {running.map((p) => (
        <span key={p.agent} className="badge badge-blue">
          <Icon as={Robot} /> {p.agent} {p.status === 'running' ? '工作中…' : ''} {p.summary ? `· ${p.summary.slice(0, 40)}` : ''}
        </span>
      ))}
    </div>
  )
}

/* ===== 只读计划提示条（批准卡超时/被拒后跨 ask 恢复） ===== */
function PlanReopenBar() {
  const planCard = useQAStore((s) => s.planCard)
  const dismissPlanCard = useQAStore((s) => s.dismissPlanCard)
  if (!planCard || planCard.status !== 'pending' || !planCard.readonly) return null
  return (
    <div style={{ marginBottom: 8, padding: '8px 12px', borderRadius: 6, background: 'var(--warning-soft)', color: 'var(--warning)', fontSize: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
      <span>有未批准的计划「{planCard.goal.slice(0, 30)}」——发送任意消息将重新打开批准卡</span>
      <button className="toolbar-btn" style={{ fontSize: 11 }} onClick={dismissPlanCard}>关闭</button>
    </div>
  )
}

/* ===== 输入框下方工具栏（v2：D5 模型切换按钮在文件列表右边） ===== */
function ToolbarBelow({ onOpenPanel }: { onOpenPanel: (p: string) => void }) {
  const openPreview = useQAStore((s) => s.openPreview)
  const previewOpen = useQAStore((s) => s.previewOpen)
  const sessions = useQAStore((s) => s.sessions)
  const sessionId = useQAStore((s) => s.sessionId)
  // 需求 3/4（2026-08-17）：当前对话 token 计费 + 人民币估算（deepseek 按时段/缓存换算；非 deepseek 未知）
  const curSession = sessions.find((s) => s.id === sessionId)
  const curTokens = curSession?.cost_tokens ?? 0
  const cost = estimateCost(curSession)
  // 2026-09-15：上下文水位（进度条，Token 计费左侧）+ 手动压缩（按钮，模型按钮右侧）
  const ctxStat = useQAStore((s) => s.ctxStat)
  const streaming = useQAStore((s) => s.streaming)
  const compacting = useQAStore((s) => s.compacting)
  const compactContext = useQAStore((s) => s.compactContext)
  const pct = Math.min(Math.max(Number(ctxStat?.pct ?? 0), 0), 100)
  const ctxColor = pct >= 85 ? '#ef4444' : pct >= 50 ? 'var(--warning)' : 'var(--primary)'
  const canCompact = !!ctxStat?.compactible && !streaming && !compacting
  const onCompact = async () => {
    const r = await compactContext()
    if (!r.ok && r.error && r.error !== '当前不可压缩') message.warning(r.error)
  }
  // 2026-09-02：数据更新徽标已移除——数据新旧口径改由 agent 查数据时经 db_notice 注入回答
  return (
    // #1（2026-08-12）：flexWrap 防窄屏（会话列展开后）按钮挤压换行错乱
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 2px', flexWrap: 'wrap' }}>
      {/* onMouseDown 阻止冒泡：避免 document 外部点击监听先关闭浮窗，导致 toggle 失效 */}
      <button className="toolbar-btn" onMouseDown={(e) => e.stopPropagation()} onClick={() => onOpenPanel('sessions')}>会话列表</button>
      <button className="toolbar-btn" onMouseDown={(e) => e.stopPropagation()} onClick={() => onOpenPanel('skills')}>ai技能</button>
      <button className="toolbar-btn" onMouseDown={(e) => e.stopPropagation()} onClick={() => onOpenPanel('mcp')}>MCP 工具</button>
      <button className="toolbar-btn" onMouseDown={(e) => e.stopPropagation()} onClick={() => onOpenPanel('files')}>文件</button>
      {/* D5：原上传按钮位置改为模型切换按钮（文件列表按钮右边） */}
      <button className="toolbar-btn" onMouseDown={(e) => e.stopPropagation()} onClick={() => onOpenPanel('models')}>模型</button>
      {/* 2026-09-15：压缩按钮（模型按钮右侧）——无"可压缩内容"时置灰；压缩为阻塞式回合 */}
      <button className="toolbar-btn" disabled={!canCompact} onClick={onCompact}
        title={compacting ? '正在压缩上下文…'
          : ctxStat?.compactible
            ? `把第 1-${Math.max((ctxStat.rounds || 0) - (ctxStat.keep_recent || 0), 0)} 轮压缩为结构化摘要（只影响发给模型的历史，界面原文保留）`
            : '暂无可压缩内容（历史太短）'}>
        {compacting ? '压缩中…' : '压缩'}
      </button>
      <div style={{ flex: 1 }} />
      {/* v2 用户反馈：浏览区打开时隐藏「浏览区」按钮（再点无意义，收起在浏览区内） */}
      {!previewOpen && (
        <button className="toolbar-btn" onMouseDown={(e) => e.stopPropagation()} onClick={openPreview}>浏览区</button>
      )}
      {/* 2026-09-15：上下文水位条（Token 计费左侧）——显示"下一轮请求预计占用"占预算百分比 */}
      {ctxStat && (ctxStat.watermark_tokens > 0 ? (
        <span className="badge badge-gray"
          title={`上下文水位：约 ${ctxStat.watermark_tokens.toLocaleString()} / ${ctxStat.budget_tokens.toLocaleString()} tokens（下一次提问时的预计上下文占用）${ctxStat.boundary ? `；已压缩至第 ${ctxStat.boundary} 轮` : ''}`}>
          上下文 {pct}%
          <span className="ctx-bar"><span className="ctx-bar-fill" style={{ width: `${pct}%`, background: ctxColor }} /></span>
        </span>
      ) : (
        // 本会话尚无水位记录（功能上线前的老会话/尚未提问）——显示"—"而非误导性的 0%
        <span className="badge badge-gray" title="本会话还没有上下文水位记录——提一次问后自动统计显示">
          上下文 —
        </span>
      ))}
      {/* 需求 3/4：token 计费框（浏览区与数据更新时间之间；deepseek 按北京时间时段+缓存换算人民币） */}
      <span className="badge badge-blue"
        title={`当前对话 token 消耗（deepseek 按时段/缓存命中换算；其他模型未知）：输入命中 ${curSession?.cost_prompt_hit ?? 0} / 未命中 ${curSession?.cost_prompt_miss ?? 0} / 输出 ${curSession?.cost_completion ?? 0}`}>
        Token 计费: {cost.known ? `${cost.text}（${curTokens.toLocaleString()}）` : '未知'}
      </span>
    </div>
  )
}

/* ===== 浮窗面板（会话列表已迁移为导航栏右侧固定列，不再走浮窗） ===== */
// 上传公共段（2026-09-17 抽出）：文件选择与**剪贴板粘贴**（Ctrl+V）共用同一套校验与链路。
// 放模块级：FloatPanel（文件浮窗）与 ChatPanel（输入框粘贴）都要用。
// onDone 给需要额外刷新自己那份列表的调用方（文件浮窗的 loadFiles）。
async function uploadPickedFiles(picked: File[], onDone?: () => void) {
  if (!picked.length) return
  const st = useQAStore.getState()
  // 2026-08-27（bug 修复）：上传前无会话时自动建会话（与发消息自动建会话对齐）——
  // 原直接拦截报"会话已失效"，部署机重启/新浏览器后必须先手动创建对话才能上传
  if (!st.sessionId) {
    try { await st.newSession() } catch { message.warning('会话已失效，请先发送一条消息以创建新会话'); return }
  }
  // C4（E-05）：超限文件前端拦截并提示（原零反馈静默失败）
  // 2026-09-15：上限按类型区分——音视频 600MB、其余 200MB（与后端 media_upload_max_mb / max_upload_size_mb 一致）
  const MEDIA_RE = /\.(mp4|mov|m4v|mkv|webm|avi|flv|wmv|3gp|mp3|wav|m4a|aac|flac|ogg|opus|wma|amr)$/i
  const limitOf = (name: string) => (MEDIA_RE.test(name) ? 600 : 200) * 1024 * 1024
  const oversize = picked.filter((f) => f.size > limitOf(f.name))
  const valid = picked.filter((f) => f.size <= limitOf(f.name))
  if (oversize.length) message.warning(`「${oversize.map((f) => f.name).join('、')}」超过上限（音视频 600MB / 其他 200MB），已跳过`)
  if (!valid.length) return
  try {
    await useQAStore.getState().uploadFiles(valid)
    onDone?.()
  } catch (e: any) {
    message.error(e?.response?.data?.error?.message || '上传失败，请重试')  // F4：失败 toast
  }
}

// 剪贴板直传（2026-09-17 用户要求）：在别处复制的文件（资源管理器里复制的 docx/图片等）或截图，
// 聚焦输入框 Ctrl+V 即直接进当前会话文件，可一次粘贴多个。纯文本粘贴不受影响（无 file 项走默认行为）。
function onPasteUpload(e: React.ClipboardEvent<HTMLTextAreaElement>) {
  const cd = e.clipboardData
  if (!cd) return
  const fromFiles = Array.from(cd.files || [])
  const fromItems = Array.from(cd.items || [])
    .filter((it) => it.kind === 'file')
    .map((it) => it.getAsFile())
    .filter((f): f is File => !!f)
  const picked = fromFiles.length ? fromFiles : fromItems
  if (!picked.length) return
  e.preventDefault()          // 有文件就不把内容塞进输入框
  void uploadPickedFiles(picked)
}

function FloatPanel({ type, onClose }: { type: string; onClose: () => void }) {
  // C14（2026-08-12）：选择器订阅——原整 store 订阅使浮窗随每个流式 token 重渲染
  const sessionId = useQAStore((s) => s.sessionId)
  const activeSkills = useQAStore((s) => s.activeSkills)
  const setActiveSkills = useQAStore((s) => s.setActiveSkills)
  const autoSkill = useQAStore((s) => s.autoSkill)
  const setAutoSkill = useQAStore((s) => s.setAutoSkill)
  const [skills, setSkills] = useState<any[]>([])
  const [deptBlocked, setDeptBlocked] = useState<string[] | null>(null) // 2026-09-01：本团队工具黑名单（null=无禁用）
  const [sessionFiles, setSessionFiles] = useState<any[]>([]) // 会话文件（type=files）
  // 2026-09-03：AI 外部工具（MCP）浮窗数据——个人启用集与「AI外部工具」页同源（user_mcp 偏好）
  const [mcpTools, setMcpTools] = useState<any[]>([])
  const [myMcp, setMyMcp] = useState<string[]>([])
  const myMcpRef = useRef<string[]>([])
  myMcpRef.current = myMcp
  const ref = useRef<HTMLDivElement>(null)
  // 三期 M15：勾选变化防抖保存到后端（按用户持久化）
  // 勾选外观已变，保存失败不能静默——否则用户以为已生效（实际没落库）
  const saveTimer = useRef<ReturnType<typeof setTimeout>>()
  const scheduleSave = () => {
    clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => {
      useQAStore.getState().saveSkillPrefs().catch((e) => message.error(errMessage(e) || '技能勾选保存失败'))
    }, 500)
  }
  // 2026-09-03：MCP 启用集防抖保存（/mcp/tools/prefs，与「AI外部工具」页同源）
  const mcpTimer = useRef<ReturnType<typeof setTimeout>>()
  const saveMcpPrefs = (ids: string[]) => {
    fetch(`${API_PREFIX}/mcp/tools/prefs`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool_ids: ids }),
    }).catch(() => message.error('外部工具启用保存失败'))
  }
  const scheduleMcpSave = () => {
    clearTimeout(mcpTimer.current)
    mcpTimer.current = setTimeout(() => saveMcpPrefs(myMcpRef.current), 500)
  }
  // N6：卸载时清理防抖计时器（防卸载后回调与内存残留）
  // C7（E-08）：有待保存的防抖 → 立即落库（原勾选后 500ms 内关面板，改动静默丢失）
  useEffect(() => {
    return () => {
      if (saveTimer.current) {
        clearTimeout(saveTimer.current)
        saveTimer.current = undefined
        useQAStore.getState().saveSkillPrefs().catch((e) => message.error(errMessage(e) || '技能勾选保存失败'))
      }
      // 2026-09-03：MCP 启用集防抖未落库时卸载立即保存（同技能偏好语义）
      if (mcpTimer.current) {
        clearTimeout(mcpTimer.current)
        mcpTimer.current = undefined
        saveMcpPrefs(myMcpRef.current)
      }
    }
  }, [])

  // 2026-09-01（黑名单语义）：团队禁用命中 → 置灰（run_script 个人级兜底豁免）
  // 原白名单语义 isDenied 在接口改 dept_blocked 后恒为 false → 置灰失效（死代码），已反转
  const isDenied = (id: string) => deptBlocked != null && deptBlocked.includes(id) && id !== 'run_script'

  const loadFiles = useCallback(() => {
    // 2026-08-27（bug 修复）：sessionId 从 store 实时读——自动建会话（newSession 异步）后本渲染闭包
    // 的 sessionId 仍是 null，原逻辑跳过 fetch → 上传成功但文件区不刷新（第二次上传才一起出现）
    const sid = useQAStore.getState().sessionId
    if (sid) {
      fetch(`${API_PREFIX}/chat/sessions/${sid}/files`)
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((d) => setSessionFiles(d.files || []))
        .catch(() => setSessionFiles([]))
    }
  }, [])

  useEffect(() => {
    // M15 起 /skills 需登录鉴权（L11：cookie 自动携带）；四期重构返回 {tools, dept_skills}，空值防御防白屏
    fetch(`${API_PREFIX}/skills`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => { setSkills(d.tools || []); setDeptBlocked(d.dept_blocked ?? null) })
      .catch(() => setSkills([]))
    // 2026-09-03：AI 外部工具（MCP）——挂载即拉（含我的启用集）
    fetch(`${API_PREFIX}/mcp/tools`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => { setMcpTools(d.tools || []); setMyMcp(d.my_enabled || []) })
      .catch(() => setMcpTools([]))
    // 会话文件列表（打开/切会话时刷新）
    if (type === 'files') loadFiles()
    // 外部点击关闭：点击浮窗外部区域关闭（按钮已 stopPropagation，不影响 toggle）
    // v2 修复：Popconfirm/Modal 的确认按钮渲染在浮窗外的 portal 里——点击「OK」先触发
    // 此处关闭浮窗 → 组件卸载 → 删除操作被吞（用户反馈"点完 ok 还是没删掉"根因）
    const handler = (e: MouseEvent) => {
      const t = e.target as Node
      // 问题 1 修复（2026-08-17）：antd Select 下拉弹层渲染在 body 级 portal（浮窗之外），
      // 点选模型/思考选项时 mousedown 落在浮窗外 → 浮窗被关闭（用户反馈"切换模型窗口消失"）
      if (t instanceof Element && t.closest('.ant-popover, .ant-modal-root, .ant-message, .ant-select-dropdown')) return
      if (ref.current && !ref.current.contains(t)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type, sessionId])

  // 删除会话文件（物理+DB，Agent 后续无法再读取）；确认由调用处 Popconfirm 处理
  const delFile = async (id: string) => {
    try {
      const r = await fetch(`${API_PREFIX}/chat/files/${id}`, {
        method: 'DELETE',
      })
      if (!r.ok) throw new Error('删除失败')
      setSessionFiles((list) => list.filter((f) => f.file_id !== id))
      // C14（2026-08-12）：删除会话文件后同步清待发列表对应项（原残留 → 下轮发送带已删 file_id → 404）
      useQAStore.getState().removeFile(id)
    } catch {
      message.error('删除失败，请稍后重试')
    }
  }

  const fmtSize = (n: number | null | undefined) => {
    const s = n || 0
    return s > 1024 * 1024 ? `${(s / 1024 / 1024).toFixed(1)}MB` : `${Math.max(1, Math.round(s / 1024))}KB`
  }

  // D5：上传入口并入文件列表浮窗
  const pickUpload = () => {
    const fi = document.createElement('input')
    fi.type = 'file'
    fi.accept = '.xlsx,.xls,.csv,.docx,.pdf,.png,.jpg,.jpeg,.html,.htm,.pptx,.ppt,.md,.txt,.zip,.mp4,.mov,.m4v,.mkv,.webm,.avi,.flv,.wmv,.3gp,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus,.wma,.amr'
    fi.multiple = true
    fi.onchange = () => { void uploadPickedFiles(Array.from(fi.files || []), loadFiles) }
    fi.click()
  }

  return (
    <div ref={ref} className="card float-panel">
      {type === 'skills' && (
        <>
          <div style={{ fontWeight: 600, padding: '12px 14px', borderBottom: '1px solid var(--border)' }}>
            ai技能
            <label style={{ float: 'right', fontSize: 12, fontWeight: 400 }} title="自动选择：自动启用全部技能">
              <input type="checkbox" checked={autoSkill} onChange={(e) => { setAutoSkill(e.target.checked); scheduleSave() }} /> 自动选择
            </label>
          </div>
          {/* 2026-08-21：单选技能机制已下线（内置技能下线后无 single 工具）——单列多选勾选
              2026-09-10 走查教训：本行 filter **静默丢弃** select_mode==='single' 的工具——
              后端一旦有工具被标成 single，就会「UI 无入口 + auto 不注入」凭空消失且零报错
              （当天实事故即如此）。后端 `tests/skill_smoke.py` 已加守卫断言「无 single 工具」
              与「外部工具必为 mcp/multi」；若将来要恢复单选 UI，务必同步去掉那条断言。 */}
          <div style={{ maxHeight: 320, overflow: 'auto' }}>
            <div style={{ fontSize: 11, color: 'var(--text-3)', padding: '4px 14px' }}>技能</div>
            <div style={{ padding: '6px 0' }}>
              {skills.filter((s) => s.select_mode !== 'single').map((s: any) => {
                const denied = isDenied(s.id)
                const on = autoSkill || activeSkills.includes(s.id)
                return (
                  <div key={s.id} className={`skill-item ${s.status !== 'active' || denied ? 'disabled' : ''}`}
                    onClick={() => { if (s.status === 'active' && !denied && !autoSkill) { setActiveSkills(activeSkills.includes(s.id) ? activeSkills.filter((x) => x !== s.id) : [...activeSkills, s.id]); scheduleSave() } }}>
                    <input type="checkbox" readOnly checked={on} disabled={autoSkill || s.status !== 'active' || denied} />
                    <span style={{ marginLeft: 8 }}><Icon as={iconFromKey(s.icon)} /> {s.name}</span>
                    {s.status !== 'active' && <span className="badge badge-gray" style={{ marginLeft: 6 }}>规划</span>}
                    {denied && <span className="badge badge-gray" style={{ marginLeft: 6 }}>本团队已禁用</span>}
                  </div>
                )
              })}
            </div>
          </div>
        </>
      )}
      {type === 'mcp' && (
        <>
          <div style={{ fontWeight: 600, padding: '12px 14px', borderBottom: '1px solid var(--border)' }}>
            AI 外部工具
            <label style={{ float: 'right', fontSize: 11, fontWeight: 400, color: 'var(--text-3)' }}>
              管理见「AI外部工具」页
            </label>
          </div>
          <div style={{ maxHeight: 320, overflow: 'auto' }}>
            <div style={{ fontSize: 11, color: 'var(--text-3)', padding: '4px 14px' }}>已启用（运维开通）</div>
            <div style={{ padding: '6px 0' }}>
              {mcpTools.filter((t) => t.status === 'active').map((t: any) => {
                const on = myMcp.includes(t.id)
                return (
                  <div key={t.id} className="skill-item"
                    onClick={() => {
                      setMyMcp(on ? myMcp.filter((x) => x !== t.id) : [...myMcp, t.id])
                      scheduleMcpSave()
                    }}>
                    <input type="checkbox" readOnly checked={on} />
                    <span style={{ marginLeft: 8 }}><Icon as={iconFromKey(t.icon)} /> {t.name}</span>
                    <span className="badge badge-gray" style={{ marginLeft: 6 }}>{on ? '对话中可用' : '已停用'}</span>
                  </div>
                )
              })}
              {mcpTools.filter((t) => t.status === 'active').length === 0 && (
                <div style={{ padding: '12px 14px', fontSize: 12, color: 'var(--text-3)' }}>暂无运维已启用的外部工具</div>
              )}
            </div>
            {mcpTools.some((t) => t.status !== 'active') && (
              <>
                <div style={{ fontSize: 11, color: 'var(--text-3)', padding: '4px 14px' }}>未开通 / 规划中</div>
                <div style={{ padding: '6px 0' }}>
                  {mcpTools.filter((t) => t.status !== 'active').map((t: any) => (
                    <div key={t.id} className="skill-item disabled">
                      <input type="checkbox" disabled />
                      <span style={{ marginLeft: 8 }}><Icon as={iconFromKey(t.icon)} /> {t.name}</span>
                      <span className="badge badge-gray" style={{ marginLeft: 'auto' }}>
                        {t.status === 'planning' ? '规划中' : '运维停用'}
                      </span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
          <p style={{ fontSize: 11, color: 'var(--text-3)', padding: '0 14px 10px' }}>
            默认关闭：勾选后智能助手才可调用该外部工具（个人开关，运维启停之外）
          </p>
        </>
      )}
      {type === 'files' && (
        <>
          <div style={{ fontWeight: 600, padding: '12px 14px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span>会话文件（{sessionFiles.length}）</span>
            {/* D5：上传按钮并入文件列表 */}
            <button className="btn-ghost" style={{ fontSize: 11, padding: '2px 10px' }} onClick={pickUpload}>＋ 上传</button>
          </div>
          <div style={{ maxHeight: 300, overflow: 'auto' }}>
            {sessionFiles.length === 0 && (
              <div style={{ padding: 16, fontSize: 12, color: 'var(--text-3)' }}>本会话暂无上传文件（点击右上角上传）</div>
            )}
            {sessionFiles.map((f) => (
              <div key={f.file_id} className="session-item" style={{ justifyContent: 'space-between' }}>
                <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {f.file_name}
                  <span style={{ color: 'var(--text-3)', fontSize: 11, marginLeft: 6 }}>{fmtSize(f.file_size)}</span>
                </div>
                <Popconfirm title={`删除文件「${f.file_name}」？删除后 Agent 将无法再读取。`}
                  onConfirm={() => delFile(f.file_id)}>
                  <button
                    className="toolbar-btn"
                    style={{ fontSize: 11, color: 'var(--error)', marginLeft: 8, flexShrink: 0 }}
                    title="删除文件（Agent 将无法再读取）"
                    onClick={(e) => e.stopPropagation()}
                  >删除</button>
                </Popconfirm>
              </div>
            ))}
          </div>
        </>
      )}
      {type === 'models' && <ModelPanel />}
    </div>
  )
}

/* ===== D3：模型切换浮窗（主对话模型 + 思考强度） =====
 * v2 用户反馈修正（2026-08-14）：辅助任务模型四个档位迁移至 AI 技能管理页卡片浮窗；
 * 思考强度按钮短文字 + 选中反馈；主对话模型用 antd Select（原生 OS 弹层黑闪问题）
 */
function ModelPanel() {
  const modelSel = useQAStore((s) => s.modelSel)
  const setModelSel = useQAStore((s) => s.setModelSel)
  const [options, setOptions] = useState<{ llm: any[]; llm_aux: any[]; vision: any[]; image: any[]; thinking: any[]; free?: { platform: string; model: string } } | null>(null)
  useEffect(() => {
    fetch(`${API_PREFIX}/models/options`).then((r) => (r.ok ? r.json() : Promise.reject())).then(setOptions).catch(() => {})
  }, [])
  // 后端 /models/options thinking = off/low/high/max（2026-09-01 三档对齐，与 admin 配置页 effort 一致）
  const THINK_OPTS = [
    { key: null, label: '默认' },
    ...((options?.thinking || []) as any[]).map((t) => ({ key: t.key, label: t.label })),
  ]
  const mainValue = modelSel.auxOverrides?.['_main'] ? `${modelSel.auxOverrides['_main'].platform}|${modelSel.auxOverrides['_main'].model}` : ''
  const mainModel = modelSel.auxOverrides?.['_main']?.model
  // 问题 2 修复（2026-08-17）：思考强度改为下拉（原按钮组）；按主模型能力决定是否显示——
  // 主模型为 deepseek 文本时显示（关/低/中/高），其余（GLM 视觉/图片等无思考概念）隐藏
  const thinkable = !mainModel || mainModel.startsWith('deepseek')
  // 2026-09-08：免费档定义（后端 /models/options 下发；onClick 闭包需外层引用）
  const freeCfg = options?.free
  return (
    <div>
      <div style={{ fontWeight: 600, padding: '12px 14px', borderBottom: '1px solid var(--border)' }}>模型选择</div>
      <div style={{ padding: '10px 14px' }}>
        {/* 2026-09-01：当前生效模型展示——手动选择刷新后持久化（qa_model_sel），空=团队配置档 */}
      <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 4 }}>
        当前生效：{mainModel ? `${mainModel}（手动选择）` : '团队配置档（默认）'}
      </div>
      <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 4 }}>主对话模型（留空=团队配置档）</div>
        {/* 2026-09-08 开发机专用：免费档一键切换（vite.config define __DEV_FREE_TOGGLE__；部署构建=0 → 死代码剔除）
            模型值由后端 /models/options 的 free 字段下发（config 唯一维护点，前端不硬编码） */}
        {__DEV_FREE_TOGGLE__ && freeCfg && (
          <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
            <button
              className="toolbar-btn"
              style={{ fontSize: 12, padding: '2px 10px' }}
              onClick={() => setModelSel({
                auxOverrides: { ...modelSel.auxOverrides, _main: { platform: freeCfg.platform, model: freeCfg.model } },
                thinking: 'off',
              })}
            >
              免费档（{freeCfg.model}）
            </button>
            <button
              className="toolbar-btn"
              style={{ fontSize: 12, padding: '2px 10px' }}
              onClick={() => setModelSel({ auxOverrides: { ...modelSel.auxOverrides, _main: undefined }, thinking: null })}
            >
              配置档
            </button>
          </div>
        )}
        {/* 2026-08-14：原生 select 换 antd Select——原生 OS 弹层在 Windows 上黑闪 0.5s 且样式不可控 */}
        <Select size="small" style={{ width: '100%', marginBottom: 12 }} value={mainValue}
          onChange={(v) => {
            if (!v) { setModelSel({ auxOverrides: { ...modelSel.auxOverrides, _main: undefined } }); return }
            const [platform, model] = String(v).split('|')
            setModelSel({ auxOverrides: { ...modelSel.auxOverrides, _main: { platform, model } } })
          }}
          options={[
            { value: '', label: '团队配置档（默认）' },
            ...(options?.llm || []).map((m: any) => ({ value: `${m.platform}|${m.model}`, label: `${m.model}（${m.platform}）` })),
          ]} />
        {thinkable && (
          <>
            <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 4 }}>思考强度（默认/关/低/中/高）</div>
            <Select size="small" style={{ width: '100%', marginBottom: 12 }}
              value={modelSel.thinking === undefined || modelSel.thinking === null ? 'default' : modelSel.thinking}
              onChange={(v) => setModelSel({ thinking: v === 'default' ? null : (v as any) })}
              options={[
                { value: 'default', label: '默认（跟随配置）' },
                ...THINK_OPTS.filter((t) => t.key !== null).map((t) => ({ value: String(t.key), label: t.label })),
              ]} />
          </>
        )}
        {/* 2026-08-14 反馈：辅助任务模型（图片识别/图片生成/记忆库/视频生成）已迁移至
            AI 技能管理页对应卡片浮窗配置（按用户持久化），此处不再展示 */}
        <div style={{ fontSize: 11, color: 'var(--text-3)' }}>辅助任务模型（图片识别/图片生成/记忆库/视频生成）请到「AI 技能管理」页对应卡片中配置</div>
      </div>
    </div>
  )
}

/* ===== 对话面板 ===== */
export default function ChatPanel() {
  const { messages, sendQuestion, streaming, creatingNew, fileNames, fileIds, clearFiles, removeFile, openPreview, setCurrentRound, switchSession, sessions, loadSessions, newSession, deleteSession, sessionId, questionState, planCard, ctxStat, compacting, uploadProg, uploadErr, cancelUpload, retryUpload, uploadSkipped, clearUploadSkipped } = useQAStore()
  const user = useAuthStore((s) => s.user)
  const [input, setInput] = useState('')
  // 2026-09-15：压缩摘要卡片展开态（消息流顶部；原文库里仍在，展开即看摘要）
  const [compactOpen, setCompactOpen] = useState(false)
  // C9（2026-08-12）：使用说明页「去真实试一试」带入示例问题（GuidePage 暂存 localStorage，QA 页挂载预填）
  useEffect(() => {
    const pending = localStorage.getItem('qa_pending_question')
    if (pending) {
      setInput(pending)
      localStorage.removeItem('qa_pending_question')
    }
  }, [])
  // 浮窗 toggle：同一按钮再点一次关闭（需求：点击一次出现，再点击一次关闭）
  const [openPanel, setOpenPanel] = useState<string | null>(null)
  // 会话列：默认收缩（用户要求），"会话列表"按钮点击展开/收缩；
  // 展开状态 localStorage 持久化——切页返回保留上次展开状态。
  // D6 方案 A（2026-08-14）：浮层抽屉化——absolute 覆盖对话区左侧 + 半透明遮罩，
  // 底部工具栏按钮像素位置不动（挤压式布局修复）；关闭三路：再点按钮/点遮罩/Esc
  const [sessionsOpen, setSessionsOpen] = useState(() => localStorage.getItem('qa_sessions_open') === '1')
  const toggleSessions = () => {
    setSessionsOpen((v) => {
      const nv = !v
      localStorage.setItem('qa_sessions_open', nv ? '1' : '0')
      return nv
    })
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && sessionsOpen) toggleSessions() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionsOpen])
  const togglePanel = (p: string) => {
    if (p === 'sessions') { toggleSessions(); return }  // 会话列不走浮窗
    setOpenPanel((cur) => (cur === p ? null : p))
  }
  const listRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const stickToBottom = useRef(true)
  // 2026-08-07/18：停止按钮走 stopStream（显式取消后台任务 POST /chat/stop；区别于断连——
  // 断连任务在后台继续，刷新/切回自动续播）
  const stopGeneration = useQAStore((s) => s.stopStream)
  const interruptedNote = useQAStore((s) => s.interruptedNote)

  // 会话列数据：挂载 + 会话切换/新建时刷新（消息变化不重拉——onDone 已在 store 内刷新标题，
  // 消息级刷新会引发会话列表重渲染拖慢滚动）
  useEffect(() => { loadSessions() }, [sessionId])
  // ③滚动卡顿修复：onJump 用 useCallback 稳定引用，避免 MessageItem(memo) 每次父渲染失效全量重渲染
  const handleJump = useCallback((roundId?: number) => {
    openPreview()
    if (roundId) setCurrentRound(roundId)
  }, [])

  // 当前流式消息内容（打字机跟随依赖；展开/收起工具过程不改变它，不触发滚动）
  const streamingContent = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].streaming) return messages[i].content
    }
    return undefined
  }, [messages])

  // 页面加载（含浏览器后台标签页被丢弃后的重载）：自动恢复上次会话，避免消息"不见"
  useEffect(() => {
    const saved = localStorage.getItem('qa_current_session')
    if (saved && !messages.length) {
      switchSession(saved).catch(() => {})
      // #10（2026-08-12）：流式中断的降级落库是异步独立任务（最多 5s 等待 + 后台继续），
      // 首次恢复拉取可能早于落库完成 → 空；3s 后若仍空重试一次
      const t = setTimeout(() => {
        if (!useQAStore.getState().messages.length) {
          switchSession(saved).catch(() => {})
        }
      }, 3000)
      return () => clearTimeout(t)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    // 2026-08-20（滚动卡顿修复① + 走查修正）：原持续 rAF loop 每帧强制 scrollTop 赋值
    // （与滚轮抢控制——用户滚动时 stickToBottom 尚未更新就被拉回，永远出不了跟随区，
    // 只能拖滚动条退出；走查实测"无法向上滚动"）。恢复"内容增长才跟随"：
    // - 流式中：依赖 streamingContent（每 token 触发）但 rAF 合并到帧末一次赋值，**不读布局**
    // - 用户上翻（stickToBottom=false）后 effect 直接 return，不再拉回
    // - 非流式（完成/历史加载）：保留原 smooth 语义
    const el = listRef.current
    if (!el) return
    if (streaming) {
      if (!stickToBottom.current) return
      const raf = requestAnimationFrame(() => {
        const e = listRef.current
        if (e && stickToBottom.current) e.scrollTop = e.scrollHeight
      })
      return () => cancelAnimationFrame(raf)
    }
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30
    if (stickToBottom.current || nearBottom) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' }) // 完成/加载：丝滑滚动
    }
  }, [messages.length, streaming, streamingContent])
  // 反问卡/批准卡出现时滚动到底（卡片是消息列表内元素）
  useEffect(() => {
    if (!questionState && !planCard) return
    stickToBottom.current = true
    requestAnimationFrame(() => {
      const el = listRef.current
      if (el) el.scrollTop = el.scrollHeight
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questionState, planCard])

  const send = () => {
    const q = input.trim()
    if (!q) return
    // 2026-09-15：压缩是独占回合——压缩期间不发（后端亦拒 409）
    if (compacting) { message.warning('正在压缩上下文，请稍候再发送'); return }
    setInput('')
    // v2 常开输入框：流式中发送 = 插话/回答（qaStore 内部分流，不再 toast 拦截）
    sendQuestion(q).catch((e: any) => {
      // 2026-08-20（P1-②）：插话发送失败 → 恢复输入框内容 + toast（原 S7 双保险吞掉一切，
      // 配合输入框先清空 = "消息消失但后端没收到"的静默失败——用户实测复现）
      if (e?.interruptFailed) {
        setInput(q)
        message.error('插话发送失败，内容已保留，请重试')
      }
    })  // S7：防未处理 rejection（sse.ts 内部已兜底，此处双保险）
  }

  // 2026-09-15（用户反馈）：待发文件条「删除」——逐个调 DELETE /chat/files/{id}（物理+DB，
  // 与会话文件列表的删除同语义），完成后清空待发列表
  // 2026-09-17（用户反馈）：待发文件条支持**逐个删除**——原来只有一个「删除」按钮，一按把待发
  // 列表里的文件**全部**真删（物理+DB），传了 5 个只想删 1 个时只能全删重传。
  // 现在：每个文件条自带 ×（删该文件，带确认）；「全部删除」只在多于 1 个文件时出现。
  const deleteOneFile = async (id: string) => {
    try {
      const r = await fetch(`${API_PREFIX}/chat/files/${id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error('delete failed')
      removeFile(id)               // 同步从待发列表移除（store 已有 action）
      message.success('已删除该文件')
    } catch {
      message.error('删除失败，请到「文件」列表重试')
    }
  }

  const deletePendingFiles = async () => {
    const ids = [...fileIds]
    let failed = 0
    for (const id of ids) {
      try {
        const r = await fetch(`${API_PREFIX}/chat/files/${id}`, { method: 'DELETE' })
        if (!r.ok) failed += 1
      } catch {
        failed += 1
      }
    }
    clearFiles()  // 无论成败都清本地待发列表（失败项可到「文件」列表重试）
    if (failed) message.error(`${failed} 个文件删除失败，请到「文件」列表重试`)
    else if (ids.length) message.success(`已删除 ${ids.length} 个文件`)
  }

  return (
    <div style={{ display: 'flex', height: '100%', overflow: 'hidden', position: 'relative' }}>
      {/* D6 会话列抽屉：absolute 覆盖对话区左侧 + 半透明遮罩（按钮像素位置不动） */}
      {sessionsOpen && (
        <div className="sessions-mask" style={{ position: 'absolute', inset: 0, background: 'rgba(0, 0, 0, 0.5)', zIndex: 30 }}
          onClick={toggleSessions} />
      )}
      <div style={{
        position: 'absolute', left: 0, top: 0, bottom: 0,
        width: 240, zIndex: 31,
        transform: sessionsOpen ? 'translateX(0)' : 'translateX(-100%)',
        transition: 'transform 0.2s ease',
        borderRight: '1px solid var(--hairline)',
        // 浮层必须近不透明：抽屉盖在对话内容之上，半透明会透出下层文字
        background: 'var(--surface-float)',
        backdropFilter: 'blur(20px) saturate(140%)',
        WebkitBackdropFilter: 'blur(20px) saturate(140%)',
        display: 'flex',
        flexDirection: 'column',
        boxShadow: sessionsOpen ? '10px 0 40px -20px rgba(0, 0, 0, 0.9)' : 'none',
      }}>
        <div style={{ padding: '10px 12px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontWeight: 600, fontSize: 13 }}>会话列表</span>
          <button className="toolbar-btn" style={{ height: 26, fontSize: 11, padding: '0 8px' }} onClick={() => newSession()} disabled={creatingNew || streaming}>+ 新建</button>
        </div>
        <div style={{ flex: 1, overflow: 'auto' }}>
          {sessions.length === 0 && <div style={{ padding: 14, fontSize: 12, color: 'var(--text-3)' }}>暂无会话</div>}
          {sessions.map((s) => (
            <div key={s.id} className={`session-item ${s.id === sessionId ? 'active' : ''}`}
              onClick={() => { switchSession(s.id); toggleSessions() }}
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {s.title}
                {s.is_readonly && <span className="badge badge-stale" style={{ marginLeft: 6 }}>只读</span>}
                {s.mode === 'complex' && <span className="badge badge-blue" style={{ marginLeft: 6, fontSize: 10 }}><Icon as={Compass} size={12} /></span>}
                {/* 需求 3：历史对话 token 计费可见（deepseek 累计；0/未知不显示） */}
                {(s.cost_tokens ?? 0) > 0 && (
                  <span className="badge badge-blue" style={{ marginLeft: 6, fontSize: 10 }} title="该会话 token 消耗">
                    {s.cost_tokens.toLocaleString()} tokens
                  </span>
                )}
              </div>
              <Popconfirm title={`删除会话「${s.title}」？删除后不可恢复。`} onConfirm={() => deleteSession(s.id)}>
                <button className="toolbar-btn" style={{ fontSize: 11, color: 'var(--error)', marginLeft: 8, flexShrink: 0 }}
                  title="删除会话（永久删除，不可恢复）"
                  onClick={(e) => e.stopPropagation()}>删除</button>
              </Popconfirm>
            </div>
          ))}
        </div>
      </div>
      {/* 聊天区 */}
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        <div
          ref={listRef}
          className="chat-list"
          onScroll={() => {
            const el = listRef.current
            // 2026-08-20（走查修正）：跟随阈值 120→30px——原 120px 窗口 + 流式持续拉回
            // 导致滚轮永远出不了跟随区（"只能拖滚动条才能退出卡底"）；30px 滚 2-3 下即退出
            if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 30
          }}
        >
          {messages.length === 0 && (
            <div className="rise-in" style={{ maxWidth: 620, margin: '64px auto 0', textAlign: 'center' }}>
              <span className="eyebrow">智能助手</span>
              <h2 style={{ fontSize: 26, fontWeight: 660, letterSpacing: '-0.02em', margin: '16px 0 10px' }}>
                要处理什么任务？
              </h2>
              <p style={{ fontSize: 13.5, color: 'var(--text-2)', lineHeight: 1.9 }}>
                {user?.dept_name ? `${user.dept_name} · ${user.username}，` : ''}
                描述你的需求，助手将自主规划、调用工具并交付结果。
              </p>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, justifyContent: 'center', marginTop: 22 }}>
                {[
                  '把知识库里的销售表按区域汇总并画柱状图',
                  '总结这份会议录音，列出待办与负责人',
                  '读一下这份 PDF，提炼要点成一份网页报告',
                  '用 Python 清洗这份表格，输出统计结果',
                ].map((q) => (
                  <button
                    key={q}
                    className="toolbar-btn"
                    style={{ height: 34, maxWidth: '100%' }}
                    onClick={() => setInput(q)}
                    title="点击填入输入框"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}
          {/* 2026-09-15：上下文压缩卡片（第 1-boundary 轮已压缩为摘要；库里原文保留，展开看摘要） */}
          {!!ctxStat?.boundary && !!ctxStat.summary && (
            <div style={{ margin: '4px 0 10px', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--surface-inset)', overflow: 'hidden' }}>
              <div
                style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 12px', cursor: 'pointer', fontSize: 12, color: 'var(--text-2)' }}
                onClick={() => setCompactOpen((v) => !v)}
                title="只影响发给模型的历史注入（更省更稳），界面与数据库仍保留全部原文"
              >
                <span><Icon as={ArrowsInLineVertical} /> 已压缩上下文：第 1-{ctxStat.boundary} 轮 → 摘要（最近 {ctxStat.keep_recent} 轮保留原文）</span>
                <span style={{ marginLeft: 'auto', flexShrink: 0 }}>{compactOpen ? '收起' : '查看摘要'}</span>
              </div>
              {compactOpen && (
                <pre style={{ margin: 0, padding: '8px 12px', borderTop: '1px solid var(--border)', whiteSpace: 'pre-wrap', fontFamily: 'inherit', fontSize: 12, color: 'var(--text-2)', maxHeight: 260, overflow: 'auto' }}>
                  {ctxStat.summary}
                </pre>
              )}
            </div>
          )}
          {messages.map((m, i) => (
            <MessageItem key={i} m={m} onJump={handleJump} />
          ))}
        </div>
        <div style={{ borderTop: '1px solid var(--border)', padding: '12px 16px', background: 'var(--surface-1)', position: 'relative' }}>
          {/* 2026-09-15：大文件分片上传进度（校验→分片→服务端合并；失败可续传/放弃） */}
          {(uploadProg || uploadErr) && (
            <UploadProgress
              progress={uploadProg || { phase: 'upload', percent: 0, text: '上传中…' }}
              error={uploadErr ?? undefined}
              onCancel={cancelUpload}
              onRetry={uploadErr ? () => { retryUpload().catch((e: any) => message.error(e?.response?.data?.error?.message || '上传失败，请重试')) } : undefined}
              style={{ marginTop: 0, marginBottom: 8 }}
            />
          )}
          {fileNames.length > 0 && (
            <div style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
              {fileNames.map((n, i) => (
                <span key={fileIds[i] || i} className="badge badge-blue"
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  文件：{n}
                  {/* 2026-09-17：逐个删除（真删：物理+DB，与会话文件列表删除同语义） */}
                  <Popconfirm
                    title="删除该文件？"
                    description="将从会话中删除（文件列表同步移除），删除后 Agent 无法再读取"
                    okText="删除" cancelText="取消" okButtonProps={{ danger: true }}
                    onConfirm={() => deleteOneFile(fileIds[i])}
                  >
                    <span title="删除该文件"
                      style={{ cursor: 'pointer', fontWeight: 700, opacity: 0.75 }}>×</span>
                  </Popconfirm>
                </span>
              ))}
              {/* 全部删除：多于 1 个文件时才给（单个用 × 即可）
                  2026-09-15（用户反馈）：原「清除」只清本地待发列表，文件仍留在会话与文件列表且无确认——
                  改为真删除（物理+DB，与会话文件列表删除同语义），带二次确认 */}
              {fileNames.length > 1 && (
                <Popconfirm
                  title="删除这些已上传文件？"
                  description="将从会话中删除（文件列表同步移除），删除后 Agent 无法再读取"
                  okText="删除" cancelText="取消" okButtonProps={{ danger: true }}
                  onConfirm={deletePendingFiles}
                >
                  <button className="toolbar-btn" style={{ fontSize: 11 }}>全部删除</button>
                </Popconfirm>
              )}
            </div>
          )}
          {/* 2026-08-24：会话只读机制已废除（同步后不再冻结会话，改注入「数据库已更新」提示）——引导条随机制下线 */}
          {/* v2：只读计划重开提示条 */}
          <PlanReopenBar />
          {/* v3（2026-08-18）：服务重启中断提示条（任务被标记 interrupted，需重新发送） */}
          {interruptedNote && (
            <div style={{ marginBottom: 8, padding: '8px 12px', borderRadius: 6, background: 'var(--warning-soft)', color: 'var(--warning)', fontSize: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span>{interruptedNote}</span>
              <button className="toolbar-btn" style={{ fontSize: 11, marginLeft: 8, flexShrink: 0 }} onClick={() => useQAStore.setState({ interruptedNote: '' })}>知道了</button>
            </div>
          )}
          <ProgressBadges />
          {/* v2：反问浮窗卡（输入框上方；输入框不锁死，可继续打字随回答一并提交） */}
          <QuestionCard />
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
            <ModeSwitch />
            {/* #6（2026-08-12 演示修正）：按输入动态增高，最高 6 行封顶内部滚动——
                空态矮（1 行），随换行增多增高（用户明确：不是定死 6 行） */}
            <textarea
              ref={inputRef}
              className="chat-input"
              placeholder={compacting ? '正在压缩上下文…（完成后可继续对话）' : '输入问题，Enter 发送（Shift+Enter 换行）'}
              value={input}
              disabled={compacting}
              rows={Math.min(6, Math.max(1, input.split('\n').length))}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send() } }}
              onPaste={onPasteUpload}
            />
            {/* 问题 9 修复（2026-08-17）：流式中发送按钮保留（发送=插话 interrupt，底层机制
                已存在）；与停止按钮共存——原实现互斥替换导致流式中无法发送 */}
            <button className="btn-primary" onClick={send} disabled={!input.trim() || compacting}
              title={compacting ? '正在压缩上下文…' : streaming ? '发送（流式中=插话）' : '发送'}>发送</button>
            {streaming && (
              <button className="btn-ghost" onClick={stopGeneration} style={{ whiteSpace: 'nowrap' }}>■ 停止</button>
            )}
          </div>
          <TodoList />
          <ToolbarBelow onOpenPanel={togglePanel} />
          {openPanel && <FloatPanel type={openPanel} onClose={() => setOpenPanel(null)} />}
        </div>
      </div>
      {/* v2：计划批准卡（唯一人工门） */}
      <PlanApprovalCard />
      {/* 2026-09-17（用户要求）：zip 直传里被安全规则跳过的成员——小浮窗给用户看一眼，点确认关闭。
          跳过的文件**没有落盘**（服务器上不出现可执行文件），解压目录里也有一份《上传跳过清单.md》 */}
      <Modal
        open={uploadSkipped.length > 0}
        title={`已跳过 ${uploadSkipped.length} 个文件`}
        okText="知道了"
        cancelButtonProps={{ style: { display: 'none' } }}
        onOk={clearUploadSkipped}
        onCancel={clearUploadSkipped}
        width={480}
      >
        <p style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 8 }}>
          这些文件按安全规则没有导入（可执行/脚本载荷与 macOS 元数据垃圾），其余文件已正常上传。
          平台上不会保存可执行文件；确需它们时请单独确认来源后人工处理。
        </p>
        <div style={{ maxHeight: 220, overflow: 'auto', border: '1px solid var(--border)', borderRadius: 6 }}>
          {uploadSkipped.map((x, i) => (
            <div key={i} style={{ fontSize: 12, padding: '6px 10px', borderTop: i ? '1px solid var(--border)' : 'none' }}>
              <div style={{ color: 'var(--text-1)', wordBreak: 'break-all' }}>{x.name}</div>
              <div style={{ color: 'var(--text-3)' }}>{x.rule} · 来自 {x.zip}</div>
            </div>
          ))}
        </div>
      </Modal>
    </div>
  )
}
