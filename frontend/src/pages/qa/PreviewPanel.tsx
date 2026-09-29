// 预览浏览区：轮次导航 + 产出物 Tab + 渲染（对照原型 rev2 预览面板）
import { memo, useEffect, useRef, useState } from 'react'
import { message } from 'antd'
import ReactECharts from 'echarts-for-react'
import { CaretDown, CaretLeft, CaretRight, CaretUp, DownloadSimple, FileText, X } from '@phosphor-icons/react'
import client, { errMessage } from '../../api/client'
import { useQAStore } from '../../stores/qaStore'
import { sanitizeChartOption, withDarkChartTheme } from '../../lib/chartSanitize'  // SEC-24 + 深色主题默认
import { downloadByUrl } from '../../lib/download'  // 2026-09-15：大文件直链下载
import Icon from '../../components/Icon'

// 受保护文件加载：fetch 同源 → blob → 本地 URL（L11：cookie 自动携带，产出物接口不再公开）
// 2026-08-20（P5）：加载失败不再静默——原 catch 只显示"加载失败"文字，
// 任何瞬时 401/网络抖动用户零感知（走查实测"不能读取"）
function useAuthedFile(path: string) {
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const urlRef = useRef('')
  useEffect(() => {
    let cancelled = false
    if (urlRef.current) { URL.revokeObjectURL(urlRef.current); urlRef.current = '' }  // L12：旧 URL 先 revoke
    setUrl('')
    fetch(path)  // L11：token 改 httpOnly cookie，同源自动携带
      .then((r) => {
        if (!r.ok) throw new Error('无权访问')
        const cd = r.headers.get('content-disposition') || ''
        // B10：优先 RFC 5987 filename*=UTF-8''（中文名），回退旧 filename="..."
        const mStar = cd.match(/filename\*=UTF-8''([^;]+)/)
        if (mStar) {
          try { setName(decodeURIComponent(mStar[1])) } catch { setName(mStar[1]) }
        } else {
          const m = cd.match(/filename="?([^";]+)/)
          if (m) setName(m[1])
        }
        return r.blob()
      })
      .then((b) => { if (!cancelled) { const u = URL.createObjectURL(b); urlRef.current = u; setUrl(u) } })
      .catch((e) => { if (!cancelled) { setName('加载失败'); console.error('产出加载失败', path, e); message.error('文件加载失败，请重试') } })
    return () => { cancelled = true; if (urlRef.current) { URL.revokeObjectURL(urlRef.current); urlRef.current = '' } }
  }, [path])
  return { url, name }
}

// 图表 PNG 导出（二期）：POST /chat/charts/{id}/png → download_url → JWT fetch blob 下载
// 2026-08-20（滚动卡顿修复④）：memo——流式中 onText 只改 messages（rounds/option 引用不变），
// memo 生效后 ECharts 不再随每 token 全量 setOption 重绘（canvas 重栅格化是几十 ms 级放大器）
const ChartView = memo(function ChartView({ option, chartId, label }: { option: any; chartId?: string; label?: string }) {
  const [busy, setBusy] = useState(false)
  // 2026-08-18：容器宽度自适应——拖拽分栏（previewWidth 变化）/切换文件时 ECharts 不重绘，
  // 图表被挤到右侧不可见（window resize 不触发；ResizeObserver 监听容器级变化）
  const chartRef = useRef<any>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(() => { chartRef.current?.getEchartsInstance()?.resize() })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const downloadPng = async () => {
    if (!chartId) return message.warning('该图表无导出标识')
    setBusy(true)
    try {
      const { download_url } = await client.post(`/chat/charts/${chartId}/png`).then((r) => r.data)
      // 2026-09-15：直链下载（cookie 鉴权同源，流式落盘）——原 fetch→blob
      downloadByUrl(download_url, label ? `${label}.png` : undefined)
    } catch (e) {
      message.error(errMessage(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 4 }}>
        <button className="toolbar-btn" onClick={downloadPng} disabled={busy} style={{ fontSize: 11 }}>
          <Icon as={DownloadSimple} /> PNG{busy ? '…' : ''}
        </button>
      </div>
      {/* SEC-24：option 渲染前净化（删 on* 事件钩子/javascript: 串/原型污染键） */}
      <div ref={wrapRef} style={{ width: '100%' }}>
        <ReactECharts ref={chartRef} option={withDarkChartTheme(sanitizeChartOption(option))} style={{ height: 380, width: '100%' }} notMerge />
      </div>
    </div>
  )
})

/** 产出 URL 末段 → 可读文件名。
 *  output_url 对文件名做了百分号编码（故意：含 #/? 的名字会让 URL 被截断，见 core/url_utils），
 *  直接当显示名会是一串 %E6%9C%88%E5%88%86…；解码失败（脏数据）就原样返回，不抛。 */
function decodeName(s: string): string {
  try { return decodeURIComponent(s) } catch { return s }
}

// 文档在线预览（二期）：/chat/preview 转 HTML → fetch blob（L11：cookie 自动携带）→ iframe（blob URL 无需鉴权头）
// 2026-08-20（P6 收尾·用户决策）：pptx/pdf 在线预览**关闭**——部署形态预览链路反复受阻
// （iframe sandbox 白屏 / pdfjs worker .mjs MIME / 浏览器缓存旧 MIME 头），用户决定这两类
// 只提供下载；docx/xlsx/html 预览保留（iframe 链路正常）
function DocView({ filePath, label }: { filePath: string; label?: string }) {
  // 2026-09-15：不再整包 fetch 该文件（原 useAuthedFile 把大文档/zip 整个读进内存只为取下载链接）——
  // 改直链下载；可读名由调用方传 label（下载文件名仍由服务端 Content-Disposition 给）
  const [previewUrl, setPreviewUrl] = useState('')
  const [loading, setLoading] = useState(false)
  // 2026-08-31：zip 无在线预览（压缩包只能下载）——原仅禁 pdf/pptx，zip 产出会误显示"预览"入口
  const previewDisabled = /\.pdf$/i.test(filePath) || /\.pptx?$/i.test(filePath) || /\.zip$/i.test(filePath)
  // 2026-08-18：切换文件时清空旧预览状态（原 state 残留 → 新文件显示旧预览/卡在转换中）
  useEffect(() => {
    setPreviewUrl('')
    setLoading(false)
  }, [filePath])
  const openPreview = async () => {
    const parts = filePath.split('/')  // /api/v1/outputs/{session}/{round}/{file}
    const sessionId = parts[parts.length - 3]
    const roundId = Number(parts[parts.length - 2])
    setLoading(true)
    try {
      const { preview_url } = await client.post('/chat/preview', { session_id: sessionId, round_id: roundId, file_path: filePath }).then((r) => r.data)
      const b = await fetch(preview_url)  // L11：cookie 自动携带
        .then((r) => { if (!r.ok) throw new Error('预览加载失败'); return r.blob() })
      // C7（2026-08-12）：创建新 blob URL 前 revoke 旧值（原只增不减 → 每次刷新预览泄漏一个）
      setPreviewUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev)
        return URL.createObjectURL(b)
      })
    } catch (e) {
      message.error(errMessage(e))
    } finally {
      setLoading(false)
    }
  }
  // C7：卸载清理（useAuthedFile 已处理自身的 URL；previewUrl 在此兜底）
  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl) }, [])
  // 2026-09-18 走查：原先这里是一行「文档已生成：<a>文件名</a>」，两个问题——
  // ① 文件名取自 URL 末段，而 output_url 是**故意百分号编码**的（含 #/? 的名字会让 URL 被
  //    浏览器截断，见 core/url_utils）→ 直接显示是 `9%E6%9C%88%E5%88%86%E4%BA%AB%E4%BC%9A-…zip`，
  //    用户认不出是什么；② 裸 <a> 走浏览器默认蓝链，与全站风格不搭。
  // 改为「使用说明页」那套文件卡（.file-card，已提到 theme.css 共用）+ 解码后的文件名。
  const fileName = decodeName(label || filePath.split('/').pop() || '文件')
  return (
    <div style={{ padding: 20, color: 'var(--text-2)', fontSize: 12 }}>
      <div className="file-card">
        <span className="file-card-icon"><Icon as={FileText} size={24} /></span>
        <div className="file-card-body">
          <div className="file-card-name" title={fileName}>{fileName}</div>
        </div>
        <div className="file-card-actions">
          <a className="file-card-btn" href={filePath} download={fileName}>
            <Icon as={DownloadSimple} size={13} /> 下载
          </a>
          {!previewDisabled && (
            <button className="toolbar-btn" onClick={openPreview} disabled={loading} style={{ fontSize: 11, height: 28 }}>
              {previewUrl ? '刷新预览' : (loading ? '转换中…' : '在线预览')}
            </button>
          )}
        </div>
      </div>
      {previewDisabled && (
        <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-3)' }}>
          {/\.(pptx?|pdf|zip)$/i.test(filePath) ? 'PPT / PDF / 压缩包暂不支持在线预览，请下载查看' : '该格式暂不支持在线预览，请下载查看'}
        </div>
      )}
      {loading && !previewUrl && (
        <div style={{ marginTop: 8, fontSize: 11, color: 'var(--brand-ink)' }}>正在转换文档格式，请稍候…（转换结果将缓存，下次秒开）</div>
      )}
      {previewUrl && (
        // SEC-04：iframe sandbox=allow-scripts（不透明源，预览文档脚本无法触碰父页面/读 cookie；
        // 不带 allow-same-origin——blob 预览以独立源运行，与后端 html_report 转义构成双保险）
        <iframe
          src={previewUrl}
          title="文档预览"
          sandbox="allow-scripts"
          style={{ width: '100%', height: 480, border: '1px solid var(--border)', borderRadius: 8, marginTop: 10, background: 'var(--surface-1)' }}
        />
      )}
    </div>
  )
}

function ImageView({ filePath }: { filePath: string }) {
  const { url, name } = useAuthedFile(filePath)
  if (!url) return <p style={{ color: 'var(--text-3)', fontSize: 12 }}>图片加载中…</p>
  return (
    <div>
      <img
        src={url}
        alt="生成图片"
        loading="lazy"
        style={{ maxWidth: '100%', borderRadius: 8, border: '1px solid var(--border)' }}
      />
      {/* 问题 6/12（2026-08-17）：图片下载按钮（blob URL 直下；文件名=产出名） */}
      <div style={{ marginTop: 6, textAlign: 'right' }}>
        {/* 2026-08-20：去掉 display:'inline-block' 内联覆盖（toolbar-btn inline-flex 居中） */}
        <a className="toolbar-btn" style={{ fontSize: 11, textDecoration: 'none' }}
          href={url} download={name || 'image.png'}><Icon as={DownloadSimple} /> 下载图片</a>
      </div>
    </div>
  )
}

// 2026-08-10：视频产出物播放（fetch blob 带 cookie 鉴权 → objectURL → video 标签）
function VideoView({ filePath }: { filePath: string }) {
  const { url, name } = useAuthedFile(filePath)
  if (!url) return <p style={{ color: 'var(--text-3)', fontSize: 12 }}>视频加载中…</p>
  return (
    <div>
      <video
        src={url}
        controls
        preload="metadata"
        style={{ maxWidth: '100%', borderRadius: 8, border: '1px solid var(--border)', background: '#000' }}
      />
      {/* 问题 6/12（2026-08-17）：视频下载按钮 */}
      <div style={{ marginTop: 6, textAlign: 'right' }}>
        {/* 2026-08-20：去掉 display:'inline-block' 内联覆盖（toolbar-btn inline-flex 居中） */}
        <a className="toolbar-btn" style={{ fontSize: 11, textDecoration: 'none' }}
          href={url} download={name || 'video.mp4'}><Icon as={DownloadSimple} /> 下载视频</a>
      </div>
    </div>
  )
}

function TableView({ rows }: { rows: any[] }) {
  if (!rows?.length) return <p style={{ color: 'var(--text-3)', padding: 20 }}>无表格数据</p>
  const cols = Object.keys(rows[0])
  const truncated = rows.length > 50
  return (
    <div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr>{cols.map((c) => <th key={c} style={{ borderBottom: '1px solid var(--hairline)', padding: 6, textAlign: 'left' }}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.slice(0, 50).map((r, i) => (
            <tr key={i}>{cols.map((c) => <td key={c} style={{ borderBottom: '1px solid var(--hairline)', padding: 6 }}>{String(r[c] ?? '')}</td>)}</tr>
          ))}
        </tbody>
      </table>
      {/* C20（2026-08-12）：截断提示（原静默只显示前 50 行） */}
      {truncated && <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 6 }}>仅展示前 50 行（共 {rows.length} 行）</p>}
    </div>
  )
}

export default function PreviewPanel() {
  const { rounds, currentRound, setCurrentRound, closePreview, previewWidth, setPreviewWidth } = useQAStore()
  const [collapsed, setCollapsed] = useState(false) // 顶部导航/Tab 列表收纳（防遮挡内容区）
  const [activeIdx, setActiveIdx] = useState(0)      // 当前查看的产出物下标（点击文字切换）
  const tabListRef = useRef<HTMLDivElement>(null)    // 上面的文字列表（滚轮横滚目标）
  const round = rounds.find((r) => r.round_id === currentRound) ?? rounds[rounds.length - 1]

  // 切换轮次：重置选中与列表滚动位置（hooks 必须在条件 return 之前）
  useEffect(() => {
    setActiveIdx(0)
    if (tabListRef.current) tabListRef.current.scrollLeft = 0
  }, [round?.round_id])

  // 拖拽把手（空态/满态共用——原空态缺拖拽，用户反馈"分界不能拖拽"根因）
  const dragHandle = (
    <div style={{ width: 6, cursor: 'col-resize', background: 'transparent', flexShrink: 0 }}
      onMouseDown={(e) => {
        const startX = e.clientX
        const startW = previewWidth
        const move = (ev: MouseEvent) => setPreviewWidth(startW + (startX - ev.clientX))
        const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up) }
        document.addEventListener('mousemove', move)
        document.addEventListener('mouseup', up)
        // L13：mouseup 丢失（拖出窗口/拖动中卸载）时兜底清理——10s 后强制移除
        window.setTimeout(() => {
          document.removeEventListener('mousemove', move)
          document.removeEventListener('mouseup', up)
        }, 10_000)
      }}
    />
  )

  if (!round || round.outputs.length === 0) {
    // #5（2026-08-12 演示修正）：无产出物显示空态提示——原展示平台预置示例文件，
    // 演示内容应放使用说明页的演示区，不应出现在问答界面的浏览区
    return (
      <>
        {dragHandle}
        <div style={{ width: previewWidth, minWidth: 280, maxWidth: 800, background: 'var(--surface-1)', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', alignItems: 'center', padding: '10px 14px', borderBottom: '1px solid var(--border)' }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>浏览区</span>
            <div style={{ flex: 1 }} />
            <button className="toolbar-btn" onClick={closePreview}><Icon as={X} /> 收起</button>
          </div>
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-3)', fontSize: 12, padding: 20, textAlign: 'center' }}>
            暂无产出物——AI 生成图表、报告与文件后会显示在这里
          </div>
        </div>
      </>
    )
  }
  const idx = rounds.findIndex((r) => r.round_id === round.round_id)
  const chartCount = round.outputs.filter((o) => o.type === 'chart').length
  const fileCount = round.outputs.length - chartCount
  const typeName = (t: string) => (t === 'chart' ? '图表' : t === 'image' ? '图片' : t === 'doc' ? '文档' : t === 'video' ? '视频' : '文件')
  const current = round.outputs[activeIdx]  // 内容区单视图：仅显示当前选中的产出物

  // 悬停文字列表滚轮 → 横向滚动上面的文字列表（下面的内容不动，点击文字才切换）
  // C6（2026-08-12）：React 17+ wheel 是被动监听，onWheel 里 preventDefault 无效——
  // 改原生 addEventListener({passive:false}) 挂 tabListRef
  useEffect(() => {
    const el = tabListRef.current
    if (!el) return
    const onWheel = (e: globalThis.WheelEvent) => {
      e.preventDefault()
      el.scrollLeft += (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) * 1.2
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  return (
    <>
      {dragHandle}
      <div style={{ width: previewWidth, minWidth: 280, maxWidth: 800, background: 'var(--surface-1)', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}>
        {collapsed ? (
          // 收纳窄条：点击展开完整导航与列表
          <div style={{ display: 'flex', alignItems: 'center', padding: '6px 10px', borderBottom: '1px solid var(--border)', cursor: 'pointer' }}
            onClick={() => setCollapsed(false)}>
            <button className="toolbar-btn" style={{ height: 28, padding: '0 8px', fontSize: 11 }}
              onClick={(e) => { e.stopPropagation(); setCurrentRound(rounds[Math.max(0, idx - 1)].round_id) }} disabled={idx <= 0}><Icon as={CaretLeft} /></button>
            <span style={{ fontSize: 12, margin: '0 8px' }}>第 {idx + 1} 轮</span>
            <button className="toolbar-btn" style={{ height: 28, padding: '0 8px', fontSize: 11 }}
              onClick={(e) => { e.stopPropagation(); setCurrentRound(rounds[Math.min(rounds.length - 1, idx + 1)].round_id) }} disabled={idx >= rounds.length - 1}><Icon as={CaretRight} /></button>
            <span style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 10 }}>
              {chartCount} 图表{fileCount ? ` · ${fileCount} 文件` : ''}
            </span>
            <div style={{ flex: 1 }} />
            <span className="toolbar-btn" style={{ height: 28, padding: '0 8px', fontSize: 11 }}><Icon as={CaretUp} /> 展开列表</span>
          </div>
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'center', padding: '10px 14px', borderBottom: '1px solid var(--border)' }}>
              <button className="toolbar-btn" onClick={() => setCurrentRound(rounds[Math.max(0, idx - 1)].round_id)} disabled={idx <= 0}><Icon as={CaretLeft} /></button>
              <span style={{ fontSize: 13, margin: '0 10px' }}>第 {idx + 1} 轮</span>
              <button className="toolbar-btn" onClick={() => setCurrentRound(rounds[Math.min(rounds.length - 1, idx + 1)].round_id)} disabled={idx >= rounds.length - 1}><Icon as={CaretRight} /></button>
              <div style={{ flex: 1 }} />
              <button className="toolbar-btn" onClick={() => setCollapsed(true)} title="收纳顶部导航与产出物列表，留出空间查看图表/文档"><Icon as={CaretDown} /> 收纳列表</button>
              <button className="toolbar-btn" onClick={closePreview} style={{ marginLeft: 6 }}><Icon as={X} /> 收起</button>
            </div>
            {/* 产出物列表：横向滑动（不换行不遮挡），单个超长标题省略；
                悬停滚轮 → 横向滚动此列表（下面的内容不动）；点击文字 → 切换显示对应产出物 */}
            <div ref={tabListRef} style={{ display: 'flex', gap: 4, padding: '8px 14px 0', borderBottom: '1px solid var(--border)', overflowX: 'auto', whiteSpace: 'nowrap' }}>
              {round.outputs.map((o, i) => (
                <button key={`${o.type}-${i}`} className={`output-tab ${activeIdx === i ? 'active' : ''}`}
                  style={{ flexShrink: 0, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }}
                  onClick={() => setActiveIdx(i)}>
                  {typeName(o.type)} {o.label || ''}
                </button>
              ))}
            </div>
          </>
        )}
        <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
          {current ? (
            <div>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>{current.label}</div>
              {current.type === 'chart' && <ChartView option={current.option} chartId={current.chart_id} label={current.label} />}
              {current.type === 'image' && <ImageView filePath={current.file_path} />}
              {current.type === 'doc' && <DocView filePath={current.file_path} label={current.label} />}
              {current.type === 'table' && <TableView rows={current.rows} />}
              {/* 2026-08-10：file 类型按扩展名兜底——图片直显（不走文档预览转换，OfficeCLI 不支持 jpg/png） */}
              {current.type === 'file' && /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(current.file_path || '')
                ? <ImageView filePath={current.file_path} />
                : current.type === 'file' && <DocView filePath={current.file_path} label={current.label} />}
              {current.type === 'video' && <VideoView filePath={current.file_path} />}
            </div>
          ) : (
            <p style={{ color: 'var(--text-3)' }}>暂无产出</p>
          )}
        </div>
      </div>
    </>
  )
}
