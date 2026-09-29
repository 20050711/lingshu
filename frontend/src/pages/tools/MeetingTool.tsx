// 定制化工具-会议纪要（2026-08-25，需求 docs/会积纪要工具.md）
// 录音（麦克风 + 系统声音双音源混流）或上传已有音频 → 自动上传 → FunASR 转写（[mm:ss] 时间戳）
// → 场景化 LLM 总结（会议纪要/需求沟通/项目复盘/面试评估 + 自定义提示词）→ zip 下载（录音+转写+总结）
// 安全：录音严格由用户点击触发（getUserMedia/getDisplayMedia 授权）；secure context 外提示不可用
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Button, Descriptions, Input, List, message, Modal, Popconfirm, Select, Space, Spin, Tag, Tooltip, Typography, Upload,
} from 'antd'
import PageHeader from '../../components/PageHeader'
import DraggableModal from '../../components/DraggableModal'
import Icon from '../../components/Icon'
import {
  ArrowsClockwise, ClipboardText, DownloadSimple, FolderOpen, Microphone, NotePencil,
  PauseCircle, PlayCircle, Sparkle, Trash, UploadSimple,
} from '@phosphor-icons/react'
import { errMessage } from '../../api/client'
import { toolsApi } from '../../api/tools'
import { downloadByUrl } from '../../lib/download'
import { API_PREFIX } from '../../api/prefix'
import UploadProgress from '../../components/UploadProgress'
import {
  CHUNK_THRESHOLD, ChunkedUploadError, isAborted, resumeChunkedUpload, uploadFileChunked,
  type ChunkedUploadResume, type StagedFile, type UploadProgress as UploadProg,
} from '../../lib/chunkUpload'

const { TextArea } = Input
const AUDIO_EXTS = ['webm', 'm4a', 'wav', 'ogg', 'mp3', 'mp4', 'aac', 'flac', 'wma', 'amr', 'opus', 'aiff', 'mpeg', 'oga', '3gp']
const MAX_SIZE_MB = 500
const POLL_MS = 3000
// 2026-08-27（用户要求）：单次录音上限 120 分钟（与后端 meeting_max_duration_min 一致）——到点自动停止上传
const MAX_DURATION_S = 120 * 60

// ---- 2026-08-27（切页录音不中断）：录音状态提升到模块级——SPA 路由切换组件卸载后
// MediaRecorder 及其回调继续工作（JS 上下文不变），切回页面时从模块级恢复 UI；
// 刷新/关闭标签页才中断（浏览器安全模型，无法避免）。
// 组件内 recRefs.current 等与这些对象同引用，卸载后 onstop/兜底上传照常执行（setState 变为 no-op 不报错）。
let _rec = { mic: null as MediaRecorder | null, sys: null as MediaRecorder | null }
let _micChunks: Blob[] = []
let _sysChunks: Blob[] = []
let _streams: MediaStream[] = []
let _recTimer: number | null = null
let _recStartAt = 0
let _recGen = 0  // 录音代次：重挂载新开计时器后旧计时器检测到代次变化自毁，防双计时器/双 120 分钟触发
let _lastUploadedId = ''  // 2026-08-27：最近一次上传成功的 meeting_id（切页后旧闭包上传完成，切回时恢复展示用）
let _restoredMsgShown = 0  // 2026-08-27：恢复提示去重（同一段录音只弹一次，防 StrictMode 双挂载弹两条）
// 2026-09-04：转写完成（ready）/ 分析完成（done）自动弹浮窗去重——同一 meeting 各只自动弹一次
// （用户手动「查看」不受限；浮窗开着该条时只原位刷新不弹新窗）
const _readyPopped = new Set<string>()
const _donePopped = new Set<string>()
// 2026-09-04（走查反馈）：用户手动打开过该条记录后，后续完成态自动弹不再打扰（"关了又弹"根因）
const _viewedManually = new Set<string>()

// 2026-08-27：当前活跃组件实例的 UI setter——切页后旧闭包（onstop/兜底/轮询）通过它更新新组件状态，
// 根治"切回后停止录音 → 旧闭包 setState no-op → 界面卡死"问题
let _activeUI: {
  setMeetingId: (v: string | null) => void
  setStatus: (v: string) => void
  setPhase: (v: string) => void
  setTranscript: (v: string) => void
  setSeconds: (v: number) => void
  startPolling: (id: string) => void
  loadList: () => void
  setView: (v: any) => void   // 2026-09-04：转写/分析完成自动弹历史详情浮窗（切页后经新组件实例弹出）
  syncView: (v: any) => void  // 2026-09-04：仅当浮窗正开着该条时原位刷新（完成更新，不弹新窗）
  setHistoryOpen: (open: boolean) => void  // 2026-09-04：上传成功展开历史（转写状态在历史行）
} | null = null

// 2026-08-27：模块级录音计时器（唯一真源）。gen 代次机制：新组件接管时旧计时器下轮自毁。
const startRecTimer = (setSeconds: (s: number) => void, onLimit: () => void) => {
  const gen = ++_recGen
  if (_recTimer) { window.clearInterval(_recTimer); _recTimer = null }
  _recTimer = window.setInterval(() => {
    if (gen !== _recGen) { window.clearInterval(_recTimer!); _recTimer = null; return }
    const s = Math.round((Date.now() - _recStartAt) / 1000)
    setSeconds(s)
    if (s >= MAX_DURATION_S) {
      window.clearInterval(_recTimer!); _recTimer = null
      onLimit()
      message.info(`已达 ${MAX_DURATION_S / 60} 分钟录音上限，已自动停止并上传`)
    }
  }, 1000)
  return _recTimer
}

// 录音引擎：MediaRecorder 需要 secure context（https 或 localhost）；http://IP 会被浏览器拦截
const canRecord = () => typeof navigator !== 'undefined' && !!navigator.mediaDevices
  && typeof MediaRecorder !== 'undefined' && (window.isSecureContext ?? true)

const STATUS_MAP: Record<string, { text: string; color: string }> = {
  uploaded: { text: '已上传', color: 'default' },
  transcribing: { text: '转写中', color: 'processing' },
  ready: { text: '转写完成', color: 'blue' },  // 2026-09-04：与 done「已完成」绿色区分
  summarizing: { text: '总结中', color: 'processing' },
  done: { text: '已完成', color: 'success' },
  failed: { text: '失败', color: 'error' },
}

interface Meeting {
  meeting_id: string
  status: string
  title?: string | null
  duration_s?: number | null
  scene?: string | null
  created_at: string
  error_msg?: string | null
}

// 录音状态：idle=未开始 / recording=录音中 / processing=上传+转写+总结（轮询）/ done=完成
type Phase = 'idle' | 'recording' | 'processing' | 'done'

export default function MeetingTool() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [seconds, setSeconds] = useState(0)
  const [sysAudioOn, setSysAudioOn] = useState(true)          // 系统声音开关（关闭=仅麦克风）
  const [sysAudioSupported, setSysAudioSupported] = useState(true)
  const [secureOk] = useState(canRecord())
  const [meetingId, setMeetingId] = useState<string | null>(null)
  const [status, setStatus] = useState('')
  const [transcript, setTranscript] = useState('')
  const [scenes, setScenes] = useState<{ key: string; label: string }[]>([])
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const listRef = useRef<Meeting[]>([])   // 列表轮询比对用（内容没变就不 setState）
  listRef.current = meetings
  const [uploading, setUploading] = useState(false)
  // 2026-09-10：大文件分片上传进度 + 取消 + 中断续传（lib/chunkUpload.ts；录音双轨可达 1GB）
  const [prog, setProg] = useState<UploadProg | null>(null)
  const [upErr, setUpErr] = useState<string | null>(null)
  const [uploadFrom, setUploadFrom] = useState<'record' | 'file'>('record')  // 进度条只在实际来源处显示
  const abortRef = useRef<AbortController | null>(null)
  const retryRef = useRef<(() => void) | null>(null)
  const [title, setTitle] = useState('')
  // 2026-08-25（用户要求）：总结模型候选（llm_aux 段；选择在浮窗内 vModel——主区板块已移除）
  const [modelOptions, setModelOptions] = useState<{ llm_aux: { platform: string; model: string }[]; thinking: { key: string; label: string }[] }>({ llm_aux: [], thinking: [] })
  const [analysisOpen, setAnalysisOpen] = useState(false)
  // 2026-09-04：历史记录详情浮窗（不再顶掉主区——用户走查反馈"记录被挤到哪去了"）
  const [showHistory, setShowHistory] = useState(() => sessionStorage.getItem('__hist_meeting') === '1')  // 2026-09-04：会话级持久化（切页/刷新保持；新标签页/重登默认收起）
  const [view, setView] = useState<null | {
    id: string; status: string; transcript: string; summary: string; scene?: string;
    error_msg?: string; created_at?: string; duration_s?: number
  }>(null)
  // 2026-09-04：浮窗内生成/重新分析表单（主区处理板块已移除——场景/模型/思考强度全在浮窗选择）
  const [vEditOpen, setVEditOpen] = useState(false)
  const [vScene, setVScene] = useState<string | undefined>(undefined)
  const [vPrompt, setVPrompt] = useState('')
  const [vModel, setVModel] = useState<{ platform: string; model: string; thinking?: string | null } | null>(null)
  const [vGenerating, setVGenerating] = useState(false)

  // 2026-08-27：refs 指向模块级对象（切页卸载后录音数据/流继续存活；重挂载 useRef 重新指向同一对象）
  const recRefs = useRef(_rec)
  const micChunksRef = useRef(_micChunks)
  const sysChunksRef = useRef(_sysChunks)
  const streamsRef = useRef(_streams)
  const timerRef = useRef<number | null>(_recTimer)  // 与模块级 _recTimer 同步（兼容现有 clear 逻辑）
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
  }, [])

  const loadList = useCallback(() => {
    toolsApi.listMeetings().then((r) => {
      const list = r.meetings || []
      // 走查"页面卡"：内容没变就跳过 setState——原实现每 10 秒用新数组刷新整页
      if (JSON.stringify(list) !== JSON.stringify(listRef.current)) {
        listRef.current = list
        setMeetings(list)
      }
    }).catch(() => { /* 列表失败不打扰 */ })
  }, [])
  useEffect(() => { loadList() }, [loadList])
  // 2026-08-27（改进）：存在处理中条目（转写/总结）时列表 10s 自动刷新——历史条目转写完成后无需手动刷新页面
  useEffect(() => {
    const iv = setInterval(() => {
      setMeetings((prev) => {
        if (prev.some((m) => ['uploaded', 'transcribing', 'summarizing'].includes(m.status))) loadList()
        return prev
      })
    }, 10000)
    return () => clearInterval(iv)
  }, [loadList])

  // 场景下拉
  useEffect(() => {
    toolsApi.fetchMeetingScenes().then((r) => setScenes(r.scenes || [])).catch(() => message.error('总结场景加载失败'))
  }, [])
  // 2026-08-25：总结模型候选（llm_aux 段文本模型）
  useEffect(() => {
    fetch(`${API_PREFIX}/models/options`).then((r) => (r.ok ? r.json() : Promise.reject())).then(setModelOptions).catch(() => {})
  }, [])

  // 录音结束统一清理（流/计时器）——2026-08-27：模块级真源就地清空（禁止重新赋值 recRefs.current，
  // 否则切页重挂载后 useRef(_rec) 与旧实例 current 解耦，恢复判断失效 + 旧录音数据悬空）
  const cleanupRec = useCallback(() => {
    if (_recTimer) { window.clearInterval(_recTimer); _recTimer = null }
    if (timerRef.current) { window.clearInterval(timerRef.current); timerRef.current = null }
    streamsRef.current.forEach((s) => s.getTracks().forEach((t) => t.stop()))
    streamsRef.current.length = 0
    recRefs.current.mic = null
    recRefs.current.sys = null
  }, [])

  // 轮询详情（上传后：uploaded → transcribing → ready；总结后：summarizing → done）
  const startPolling = useCallback((id: string) => {
    stopPolling()
    pollRef.current = setInterval(() => {
      toolsApi.getMeeting(id).then((d) => {
        setStatus(d.status)
        if (d.transcript) setTranscript(d.transcript)
        if (d.status === 'ready') {
          // 2026-08-25（bug 修复）：转写完成 → 退出 processing，录音区立即可用（可再次录音）
          stopPolling()
          setPhase('idle')
          loadList()
          // 2026-09-04（用户要求）：转写完成时若用户还没离开本页面（活性组件+页面可见）→ 自动弹详情浮窗；
          // 离开页面/切后台不弹（_readyPopped 去重，同一条只弹一次；手动「查看」不受限）
          if (document.visibilityState !== 'hidden' && !_readyPopped.has(id) && !_viewedManually.has(id)) {
            _readyPopped.add(id)
            _activeUI?.setView?.({
              id, status: 'ready', transcript: d.transcript || '', summary: d.summary || '',
              scene: d.scene, error_msg: d.error_msg || '',
            })
          }
        }
        if (['done', 'failed'].includes(d.status)) {
          stopPolling()
          setPhase('done')
          if (d.status === 'failed') { message.error(`处理失败：${d.error_msg || '未知错误'}`) }
          loadList()
          // 2026-09-04（用户要求）：分析完成——浮窗仍开着该条 → 原位刷新（用户自己 ❌ 关闭，不弹新窗）；
          // 浮窗已关（用户 ❌ 过/从未开）且用户仍在页面 → 同 ready 机制自动弹一次（_donePopped 去重）
          const next = {
            id, status: d.status, transcript: d.transcript || '', summary: d.summary || '',
            scene: d.scene, error_msg: d.error_msg || '',
          }
          _activeUI?.syncView?.(next)
          if (d.status === 'done' && document.visibilityState !== 'hidden' && !_donePopped.has(id) && !_viewedManually.has(id)) {
            _donePopped.add(id)
            _activeUI?.setView?.(next)
          }
        }
      }).catch(() => { /* 单次轮询失败忽略（下轮重试） */ })
    }, POLL_MS)
  }, [loadList, stopPolling])

  // 单轨 MediaRecorder 工厂（timeslice 1s：后台期间数据持续累积，切 tab 不丢）
  const newRecorder = (stream: MediaStream, chunks: Blob[], onStop: () => void) => {
    const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find((m) => MediaRecorder.isTypeSupported(m)) || ''
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime, audioBitsPerSecond: 128000 } : undefined)
    rec.ondataavailable = (ev) => { if (ev.data.size > 0) chunks.push(ev.data) }
    rec.onstop = onStop
    rec.start(1000)
    return rec
  }

  // 开始录音：2026-08-25 双轨（不再混流）——本地麦克风一轨 + 线上系统声音一轨，天然区分两方；
  // 系统声音失败（Firefox 不支持/取消）→ 降级单轨
  const startRecording = async () => {
    if (!secureOk) { message.error('浏览器录音需要 HTTPS 或 localhost 访问，当前环境不可用'); return }
    setPhase('recording')
    setSeconds(0)
    setTranscript('')
    setMeetingId(null)
    setStatus('')
    micChunksRef.current.length = 0  // 2026-08-27：就地清空保持引用（模块级真源，重挂载后仍指向同一数组）
    sysChunksRef.current.length = 0
    const streams: MediaStream[] = []
    try {
      if (sysAudioOn && sysAudioSupported) {
        try {
          const sys = await navigator.mediaDevices.getDisplayMedia({ audio: true, video: true })
          sys.getVideoTracks().forEach((t) => t.stop())  // 只留系统音频轨
          streams.push(sys)
        } catch {
          // 2026-08-25（用户反馈）：拒绝/取消后刷新可能仍不询问（浏览器记住拒绝）——降级并提示恢复途径
          setSysAudioSupported(false)
          message.warning('系统声音不可用（已拒绝或浏览器不支持），仅录制麦克风；恢复请点下方「恢复系统声音」或浏览器地址栏左侧图标改为允许')
        }
      }
      const mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      streams.push(mic)
    } catch (e) {
      message.error('录音授权失败：' + (e instanceof Error ? e.message : '请允许麦克风权限'))
      cleanupRec()
      setPhase('idle')
      return
    }
    if (streams.length === 0) { message.error('没有可用的音频输入'); setPhase('idle'); return }
    streamsRef.current.splice(0, streamsRef.current.length)  // 2026-08-27：就地写入（模块级真源）
    streamsRef.current.push(...streams)
    try {
      const sys = streams[0]  // 先请求的为系统声音轨（若可用）
      const mic = streams[streams.length - 1]
      // 两轨独立 MediaRecorder 同时录制；两个都停后统一上传
      let stopped = 0
      const total = streams.length
      const onAllStopped = () => {
        if (++stopped < total) return
        const micBlob = new Blob(micChunksRef.current, { type: recRefs.current.mic?.mimeType || 'audio/webm' })
        const sysBlob = sys ? new Blob(sysChunksRef.current, { type: recRefs.current.sys?.mimeType || 'audio/webm' }) : undefined
        finishUpload(micBlob, sysBlob)
      }
      // 2026-08-27（切页录音不中断）：就地写入模块级真源——禁止重新赋值 recRefs.current，
      // 否则重挂载后 useRef(_rec) 回到 null，旧 recorder 数据悬空（双录/数据丢失根因）
      recRefs.current.mic = newRecorder(mic, micChunksRef.current, onAllStopped)
      recRefs.current.sys = sys ? newRecorder(sys, sysChunksRef.current, onAllStopped) : null
    } catch (e) {
      message.error('录音初始化失败：' + (e instanceof Error ? e.message : '未知错误'))
      cleanupRec()
      setPhase('idle')
      return
    }
    // 2026-08-27：计时器模块级 + 代次机制（切页重挂载后旧计时器自毁）+ 120 分钟上限自动停止
    _recStartAt = Date.now()
    timerRef.current = startRecTimer(setSeconds, stopRecording)
  }

  // 停止 → 两轨都 stop → onAllStopped 自动上传
  const stopRecording = () => {
    // 2026-08-26：立即切走录制 UI + 停计时器（防 onstop 延迟/未触发期间仍显示"录制中"）
    _activeUI?.setPhase('processing')  // 2026-08-27：120 分钟上限可能由旧计时器闭包触发 → 转发到活跃组件
    if (timerRef.current) { window.clearInterval(timerRef.current); timerRef.current = null }
    if (_recTimer) { window.clearInterval(_recTimer); _recTimer = null }
    if (recRefs.current.mic && recRefs.current.mic.state !== 'inactive') recRefs.current.mic.stop()
    if (recRefs.current.sys && recRefs.current.sys.state !== 'inactive') recRefs.current.sys.stop()
    if (!recRefs.current.mic && !recRefs.current.sys) {
      finishUpload(new Blob(micChunksRef.current, { type: 'audio/webm' }))
      return
    }
    // 2026-08-26 兜底：onstop 未触发（异常/竞态）→ 3s 后强制清理流并上传已收集数据
    // （正常路径 finishUpload 已 cleanupRec（recRefs 置空），兜底检查 mic 为空即跳过）
    window.setTimeout(() => {
      if (recRefs.current.mic) {
        const sysBlob = sysChunksRef.current.length
          ? new Blob(sysChunksRef.current, { type: 'audio/webm' })
          : undefined
        cleanupRec()
        finishUpload(new Blob(micChunksRef.current, { type: 'audio/webm' }), sysBlob)
      }
    }, 3000)
  }

  // from：上传来源（record=录音区 / file=上传区）——进度条只在实际来源处显示，避免两处重复
  const finishUpload = async (micBlob: Blob, sysBlob?: Blob, name?: string,
                              from: 'record' | 'file' = 'record',
                              resume?: { out: StagedFile[]; i: number; rs: ChunkedUploadResume }) => {
    setUploadFrom(from)
    cleanupRec()
    _activeUI?.setPhase('processing')
    const fname = name || `录音_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.webm`
    const micFile = new File([micBlob], fname, { type: micBlob.type })
    const sysFile = sysBlob ? new File([sysBlob], fname.replace('.webm', '_线上.webm'), { type: sysBlob.type }) : undefined
    const ctrl = new AbortController()
    abortRef.current = ctrl
    let retryable = false
    let staged: StagedFile | undefined
    let stagedSys: StagedFile | undefined
    const out: StagedFile[] = resume ? resume.out : []   // 已传完的轨（续传跨次保留）
    try {
      // 2026-09-10：>50MB 走分片（双轨录音可到 1GB，单请求会撞 nginx 600m / portproxy 650MB 上限）
      const needChunk = !!resume || micFile.size > CHUNK_THRESHOLD || (sysFile?.size ?? 0) > CHUNK_THRESHOLD
      setUpErr(null)
      setProg(resume
        ? { phase: 'upload', percent: 15, text: '续传中…（已传分片不重传）' }
        : { phase: 'hash', percent: 0, text: '准备上传…' })
      if (needChunk) {
        const all = sysFile ? [micFile, sysFile] : [micFile]
        for (let i = resume ? resume.i : 0; i < all.length; i++) {
          const progOf = (p: UploadProg) => setProg({
            ...p,
            percent: ((i + p.percent / 100) / all.length) * 100,
            text: all.length > 1 ? `第 ${i + 1}/${all.length} 轨：${p.text}` : p.text,
          })
          const rs = resume && i === resume.i ? resume.rs : null
          const res = rs
            ? await resumeChunkedUpload({ ...rs, opts: { ...rs.opts, signal: ctrl.signal, onProgress: progOf } })
            : await uploadFileChunked(all[i], {
                completeUrl: '/uploads/complete', signal: ctrl.signal, onProgress: progOf,
              })
          out.push({ upload_id: res.response.upload_id, file_name: res.response.file_name })
        }
        staged = out[0]
        stagedSys = out[1]
      }
      const r = await toolsApi.uploadMeeting(
        needChunk ? null : micFile,
        title,
        needChunk ? undefined : sysFile,
        { staged, stagedSys, onProgress: (p) => setProg(p) },
      )
      // 2026-08-27（切页录音不中断）：状态更新经 _activeUI 转发——旧闭包执行时更新到当前活跃组件
      //（原直接 setState：切页后旧闭包的 setState 是 no-op，UI 卡在"处理中"直到刷新）
      _lastUploadedId = r.meeting_id
      if (_activeUI) {
        _activeUI.setMeetingId(r.meeting_id)
        _activeUI.setStatus('uploaded')
        // 2026-09-04（用户要求）：上传成功即解锁录音区（phase=idle 可立即开始下一次录音）；
        // 转写中状态由历史行展示（顶部 Tag 显示空闲）
        _activeUI.setPhase('idle')
        _activeUI.startPolling(r.meeting_id)
        _activeUI.setHistoryOpen(true)
        _activeUI.loadList()
      }
    } catch (e) {
      if (e instanceof ChunkedUploadError) {
        // 2026-09-10：中断可续传——重试从失败片继续（已传轨/片不重传）；保留进度条与重试入口
        retryable = true
        setUpErr(e.message)
        const failed = e.resume
        const all = sysBlob ? [micFile, sysFile!] : [micFile]
        retryRef.current = () => finishUpload(micBlob, sysBlob, name, from,
          { out, i: Math.max(0, all.indexOf(failed.file)), rs: failed })
        _activeUI?.setPhase('idle')
      } else {
        if (isAborted(e)) message.info('已取消上传')
        else message.error('上传失败：' + errMessage(e))
        _activeUI?.setPhase('idle')
      }
    } finally {
      abortRef.current = null
      if (!retryable) setProg(null)
    }
  }

  // 上传已有音频文件（mp3/m4a/wav 等）
  const handleUpload = (file: File) => {
    const ext = (file.name.split('.').pop() || '').toLowerCase()
    if (!AUDIO_EXTS.includes(ext)) { message.error(`不支持的文件格式 .${ext}（支持 ${AUDIO_EXTS.join(' / ')}）`); return false }
    if (file.size > MAX_SIZE_MB * 1024 * 1024) { message.error(`文件超过 ${MAX_SIZE_MB}MB 上限`); return false }
    setUploading(true)
    finishUpload(file, undefined, file.name, 'file').finally(() => setUploading(false))
    return false
  }

  // 2026-08-25（用户要求）：转写稿分析（统计面板：时长/字数/说话人分布/分段数）
  const transcriptStats = (() => {
    if (!transcript) return null
    const lines = transcript.split('\n').filter((l) => l.trim())
    const speakers = new Set<string>()
    lines.forEach((l) => {
      const m = l.match(/^\s*\[\d{2}:\d{2}\]\s*([^:：]+)[:：]/)
      if (m) speakers.add(m[1].trim())
    })
    const chars = transcript.replace(/\[\d{2}:\d{2}\]/g, '').replace(/[\s\n]/g, '').length
    return {
      lines: lines.length,
      chars,
      speakers: speakers.size ? [...speakers].join(' / ') : '未标注（单说话人）',
      minutes: seconds > 0 ? `${Math.round(seconds / 60)} 分 ${seconds % 60} 秒` : '—',
    }
  })()

  // 2026-09-04：历史浮窗打开（行点「查看」/点击行）——拉详情进浮窗，主区保持现状
  const openRecord = async (m: any) => {
    _viewedManually.add(m.meeting_id)  // 手动查看过 → 完成态不再自动弹这一条
    try {
      const d = await toolsApi.getMeeting(m.meeting_id)
      setView({
        id: m.meeting_id, status: d.status, transcript: d.transcript || '',
        summary: d.summary || '', scene: d.scene, error_msg: d.error_msg || '',
        created_at: m.created_at, duration_s: m.duration_s,
      })
      // 走查：打开一条还在转写/总结中的记录时**接管轮询**——原实现只在"重新转写/重新分析"
      // 按钮里 startPolling，点开浮窗后状态永远停在打开那一刻（"完成后自动更新"是空话）。
      if (['uploaded', 'transcribing', 'summarizing'].includes(d.status)) startPolling(m.meeting_id)
    } catch (e: any) {
      // 2026-09-11（走查）：记录刚被删/列表未及刷新时点进来 → 404。自愈：刷新列表 + 软提示。
      if (e?.response?.status === 404) {
        message.info('该记录已删除')
        loadList()
        setView((prev) => (prev && prev.id === m.meeting_id ? null : prev))
      } else {
        message.error('详情加载失败：' + errMessage(e))
      }
    }
  }

  // 2026-09-04：打开新记录时重置浮窗编辑表单（数据刷新/syncView 同一 id 不重置）
  useEffect(() => {
    if (view) {
      setVEditOpen(false)
      setVScene(view.scene || undefined)
      setVPrompt('')
      setVModel(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view?.id])

  // 浮窗内生成/重新分析总结（场景/额外要求/模型/思考强度；防重复：vGenerating + status 守卫）
  const runSummary = async (id: string) => {
    if (!vScene && !vPrompt.trim()) { message.warning('请选择总结场景或输入自定义提示词'); return }
    if (vScene === 'custom' && !vPrompt.trim()) { message.warning('请先输入自定义总结要求'); return }
    if (vGenerating) return
    setVGenerating(true)
    try {
      const sceneToSend = vScene === 'custom' ? '' : (vScene || '')
      await toolsApi.summarizeMeeting(id, sceneToSend, vPrompt, vModel)
      setView((prev) => (prev && prev.id === id ? { ...prev, status: 'summarizing', scene: vScene } : prev))
      setVEditOpen(false)
      message.success('总结已开始（完成后浮窗自动更新）')
      startPolling(id)
    } catch (e) {
      message.error('总结失败：' + errMessage(e))
    } finally {
      setVGenerating(false)
    }
  }

  // 2026-08-27（用户要求）：failed 重新转写（复用已落盘音频，无需重新上传/录音）
  // 2026-09-04：参数化（浮窗 failed 重试转写复用——主区板块已移除）
  const retryTranscribe = async (id?: string) => {
    const mid = id || meetingId
    if (!mid) return
    try {
      await toolsApi.retryMeeting(mid)
      setStatus('transcribing')
      setView((prev) => (prev && prev.id === mid ? { ...prev, status: 'transcribing', error_msg: '' } : prev))
      startPolling(mid)
      message.success('已重新开始转写')
    } catch (e) {
      message.error('重新转写失败：' + errMessage(e))
    }
  }

  const deleteOne = (id: string) => {
    toolsApi.deleteMeeting(id).then(() => {
      message.success('已删除'); loadList()
      // 2026-08-27（bug 修复）：删除当前记录后清空全部详情状态——原只清 phase，总结/转写残留显示
      if (meetingId === id) {
        stopPolling(); setPhase('idle'); setMeetingId(null); setStatus(''); setTranscript('')
      }
    }).catch(() => message.error('删除失败'))
  }

  // 2026-08-27（切页录音不中断）：挂载时注册为活跃实例（旧闭包经 _activeUI 更新本组件状态）；
  // 若模块级录音仍在进行 → 恢复"录音中" UI（计时重算并接管计时器，提示按录音去重）；
  // 若切页期间旧闭包已完成上传 → 恢复"处理中"展示并接管轮询。
  // 卸载时录音中不清理（recorder 模块级存活，onstop/兜底上传照常执行），仅停轮询 + 注销活跃实例。
  useEffect(() => {
    _activeUI = {
      setMeetingId, setStatus,
      setPhase: (v) => setPhase(v as Parameters<typeof setPhase>[0]),  // Phase 联合类型包装
      setTranscript, setSeconds, startPolling, loadList,
      setView: (v) => setView(v as any),
      syncView: (v) => setView((prev) => (prev && prev.id === v.id ? v as any : prev)),
      setHistoryOpen: (open) => setShowHistory(open),
    }
    if (recRefs.current.mic && recRefs.current.mic.state !== 'inactive') {
      setPhase('recording')
      setSeconds(Math.round((Date.now() - _recStartAt) / 1000))
      timerRef.current = startRecTimer(setSeconds, stopRecording)
      if (_restoredMsgShown !== _recStartAt) {
        _restoredMsgShown = _recStartAt
        message.info('录音仍在进行（已恢复显示，可继续录音或停止上传）')
      }
    } else if (_lastUploadedId) {
      // 切页期间录音停止后旧闭包已上传成功 → 恢复处理中视图 + 接管轮询
      const mid = _lastUploadedId
      _lastUploadedId = ''
      setMeetingId(mid); setStatus('uploaded'); setPhase('processing')
      startPolling(mid)
      loadList()
    }
    return () => {
      _activeUI = null
      if (!(recRefs.current.mic && recRefs.current.mic.state !== 'inactive')) cleanupRec()
      stopPolling()
    }
  }, [cleanupRec, stopPolling, startPolling, loadList,
      setMeetingId, setStatus, setPhase, setTranscript, setSeconds])

  const fmt = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
  const processing = phase === 'processing'
  // 2026-09-10：上传进度/中断续传节点（录音区与上传区共用；中断态保留进度与重试入口）
  const progNode = prog ? (
    <UploadProgress progress={prog} error={upErr ?? undefined}
      onRetry={upErr ? () => { setUpErr(null); retryRef.current?.() } : undefined}
      onCancel={upErr
        ? () => { setUpErr(null); setProg(null); retryRef.current = null }
        : () => abortRef.current?.abort()} />
  ) : null
  const recProg = uploadFrom === 'record' ? progNode : null
  const fileProg = uploadFrom === 'file' ? progNode : null

  return (
    <div style={{ padding: '0 4px' }}>
      {/* 2026-09-04：统一页头（返回按钮独立一行 + 标题；状态 Tag 放右侧操作位） */}
      <PageHeader title={<><Icon as={Microphone} size={18} /> 会议纪要</>} backLabel="返回定制化工具"
        action={
          <Tag color={phase === 'recording' ? 'error' : (phase === 'processing' ? 'processing' : 'default')}>
            {phase === 'recording' ? '● 录音中' : phase === 'processing' ? STATUS_MAP[status]?.text || '处理中' : '空闲'}
          </Tag>
        } />
      <div style={{ maxWidth: 1124, marginInline: 'auto' }}>
      {!secureOk && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="浏览器录音需要 HTTPS 或 localhost 访问" description="当前环境无法录音，请使用 https 地址或 localhost 访问；上传已有音频文件不受影响。" />
      )}

      {/* ---- 录音 + 上传（左右两栏） ---- */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 16 }}>
        <div className="card" style={{ padding: 20 }}>
          <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 16 }}>
            <Icon as={Microphone} style={{ marginRight: 8 }} />录音
          </Typography.Title>
          <Space direction="vertical" size={14} style={{ width: '100%' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
              {phase === 'recording' ? (
                <Button type="primary" danger size="large" icon={<Icon as={PauseCircle} />} onClick={stopRecording}>停止录音</Button>
              ) : (
                // 2026-08-25（bug 修复）：done（查看历史/处理完成）后允许重新录音——仅 processing 锁定
                <Button type="primary" size="large" icon={<Icon as={PlayCircle} />} disabled={phase === 'processing'} onClick={startRecording}>开始录音</Button>
              )}
              <span style={{ fontVariantNumeric: 'tabular-nums', fontSize: 28, fontWeight: 600, color: phase === 'recording' ? '#ef4444' : '#1e293b' }}>
                {phase === 'recording' ? fmt(seconds) : '00:00'}
              </span>
              {/* 2026-08-27（用户要求）：显示 120 分钟上限——到点自动停止并上传 */}
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>上限 {MAX_DURATION_S / 60} 分钟，到点自动停止</span>
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
              {phase === 'recording'
                ? '● 录音中：本地麦克风与线上系统声音分轨录制（可切后台，持续录制）'
                : '双音源分轨录制：本地人声 + 会议软件声音，转写自动区分「本地/线上」说话人'}
            </div>
            <Space size={16} wrap>
              <label style={{ fontSize: 13, color: 'var(--text-2)', display: 'flex', alignItems: 'center', gap: 4 }}>
                {/* 2026-08-25 v2（用户反馈）：仅录音/处理中禁用勾选；idle/done（处理完成）可调整，为下次录音准备 */}
                <input type="checkbox" checked={sysAudioOn}
                  onChange={(e) => setSysAudioOn(e.target.checked)}
                  disabled={phase === 'recording' || !sysAudioSupported} />
                录制系统声音（会议软件语音等）
              </label>
              {!sysAudioSupported && (
                <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                  系统声音不可用（仅录麦克风）
                  {/* 2026-08-25（根因修复）：恢复 = 支持 + 勾选一起恢复（原来只恢复支持，sysAudioOn 仍 false 导致不生效）；
                      录音中点击提示下次生效（当前录音轨道已定）；浏览器若记住拒绝需地址栏图标改允许 */}
                  <Button size="small" type="link" style={{ padding: 0, marginLeft: 4, fontSize: 12 }}
                    onClick={() => {
                      if (phase === 'recording' || phase === 'processing') {
                        message.info('当前录音已开始，恢复将在下次开始录音时生效')
                        return
                      }
                      setSysAudioSupported(true)
                      setSysAudioOn(true)
                    }}>恢复系统声音</Button>
                </span>
              )}
            </Space>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="录音标题（可选，默认按时间命名）" maxLength={100} disabled={phase === 'recording'} />
            {/* 2026-09-04（用户要求）：上传中提示（原进度条位置；上传成功后消失——
                转写中不再显示进度条，历史行状态可见，录音区已解锁可继续下一次） */}
            {(processing || uploading || (uploadFrom === 'record' && !!upErr)) && (recProg ?? (
              <Space size={6} style={{ fontSize: 12, color: 'var(--text-2)' }}>
                <Spin size="small" /> 上传中…
              </Space>
            ))}
          </Space>
        </div>

        {/* ---- 已有音频上传区 ---- */}
        <div className="card" style={{ padding: 20 }}>
          <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 16 }}>
            <Icon as={FolderOpen} style={{ marginRight: 8 }} />上传已有音频
          </Typography.Title>
          {/* 2026-08-25（布局饱满）：上传虚线框内容填充（格式 Tag 行 + 辅助说明），消除右半空白视觉 */}
          <Upload.Dragger className="meeting-upload" accept={AUDIO_EXTS.map((e) => `.${e}`).join(',')} showUploadList={false}
            beforeUpload={handleUpload} disabled={phase !== 'idle'}>
            <p className="ant-upload-drag-icon"><Icon as={UploadSimple} size={48} className="anticon" /></p>
            <p className="ant-upload-text">点击或拖拽音频文件到此处</p>
            <p className="ant-upload-hint">mp3 / m4a / wav / webm / ogg / flac 等，≤{MAX_SIZE_MB}MB，多说话人自动聚类</p>
            <div style={{ marginTop: 6, display: 'flex', gap: 4, justifyContent: 'center', flexWrap: 'wrap' }}>
              {['mp3', 'm4a', 'wav', 'webm', 'ogg', 'flac'].map((f) => (
                <Tag key={f} style={{ marginInlineEnd: 0, fontSize: 11 }}>{f}</Tag>
              ))}
            </div>
          </Upload.Dragger>
          <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 10, lineHeight: 1.6 }}>
            会议录音 / 访谈 / 培训音频均可直接上传；转写自动区分说话人与时间，无需浏览器授权。
          </div>
          {(uploading || (uploadFrom === 'file' && !!upErr)) && (fileProg ?? <Spin size="small" style={{ marginTop: 8 }} />)}
        </div>
      </div>


        {/* ---- 历史记录（2026-09-04：统一「标题行 + 收起/展开 + 列表」，行「查看」浮窗） ---- */}
        <div style={{ marginTop: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <strong style={{ fontSize: 14 }}>历史记录（{meetings.length}）</strong>
            <Space size={8}>
              {showHistory && (
                <Popconfirm title={`清空历史记录？`} description={`将删除全部 ${meetings.filter((m) => ['done', 'ready', 'failed'].includes(m.status)).length} 条已完成记录（含录音文件）`}
                  onConfirm={async () => {
                    // 2026-09-11：原实现"发完删除就立刻 loadList"——删除还没落库，列表刷回来仍是旧行，
                    // 点进去就是 404。改为等全部删除完成再刷新。
                    const finals = meetings.filter((m) => ['done', 'ready', 'failed'].includes(m.status))
                    await Promise.allSettled(finals.map((m) => toolsApi.deleteMeeting(m.meeting_id)))
                    message.success(`已清空 ${finals.length} 条历史记录`)
                    loadList()
                  }}>
                  <Button size="small" type="link" danger disabled={!meetings.some((m) => ['done', 'ready', 'failed'].includes(m.status))}>清空历史</Button>
                </Popconfirm>
              )}
              <Button size="small" type="link" style={{ padding: 0 }} onClick={() => setShowHistory((v) => { const nxt = !v; sessionStorage.setItem('__hist_meeting', nxt ? '1' : '0'); return nxt })}>
                {showHistory ? '收起' : '展开'}
              </Button>
            </Space>
          </div>
          {showHistory && (
              <List
                size="small"
                dataSource={meetings}
                locale={{ emptyText: '暂无录音记录' }}
                renderItem={(m) => (
                  <List.Item style={{ cursor: 'pointer' }} onClick={() => ['done', 'ready', 'failed'].includes(m.status) && openRecord(m)}
                    actions={[
                    ['done', 'ready', 'failed'].includes(m.status) && <Button size="small" type="link" onClick={(e) => { e.stopPropagation(); openRecord(m) }}>查看</Button>,
                    // 2026-09-11（走查：删完又弹"详情加载失败"）：Popconfirm 的确认按钮在
                    // portal 里渲染，点击事件仍沿 **React 树**冒泡到整行 onClick → 对刚删除的
                    // 记录 openRecord → 404。按钮自身的 stopPropagation 管不到 portal，
                    // 必须在 Popconfirm 外层包一层（React 树上的父节点）拦截。
                    <span key="del" onClick={(e) => e.stopPropagation()}>
                      <Popconfirm title="删除该录音？" onConfirm={() => deleteOne(m.meeting_id)}>
                        <Button size="small" type="text" danger icon={<Icon as={Trash} />} />
                      </Popconfirm>
                    </span>,
                  ].filter(Boolean)}>
                    <List.Item.Meta
                      title={<span>{m.title || '会议录音'}
                        <Tag color={STATUS_MAP[m.status]?.color || 'default'} style={{ marginLeft: 8 }}>{STATUS_MAP[m.status]?.text || m.status}</Tag>
                        {/* 2026-08-27（改进）：非终态条目提示——列表 10s 自动刷新，完成后可查看 */}
                        {['uploaded', 'transcribing', 'summarizing'].includes(m.status) &&
                          <span style={{ fontSize: 11, color: 'var(--text-3)' }}>处理中，稍后自动刷新查看</span>}
                      </span>}
                      description={`${m.created_at?.slice(0, 16).replace('T', ' ')}${m.duration_s ? ` ｜ ${Math.round(m.duration_s / 60)} 分钟` : ''}${m.error_msg ? ` ｜ ${m.error_msg.slice(0, 40)}` : ''}`}
                    />
                  </List.Item>
                )}
              />
          )}
        </div>

        {/* ---- 2026-08-25：转写稿分析（统计面板） ---- */}
        <Modal title="转写稿分析" open={analysisOpen} onCancel={() => setAnalysisOpen(false)} footer={null} width={560}>
          {transcriptStats && (
            <>
              <Descriptions size="small" column={2} style={{ marginBottom: 12 }}>
                <Descriptions.Item label="录音时长">{transcriptStats.minutes}</Descriptions.Item>
                <Descriptions.Item label="转写字数">{transcriptStats.chars} 字</Descriptions.Item>
                <Descriptions.Item label="分段数">{transcriptStats.lines} 段</Descriptions.Item>
                <Descriptions.Item label="说话人">{transcriptStats.speakers}</Descriptions.Item>
              </Descriptions>
              <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 8 }}>
                说话人标签说明：双轨录音自动标注「本地/线上」，多说话人自动聚类为 A/B/C；单轨未标注表示仅一个说话人。
              </div>
              <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, maxHeight: 320, overflow: 'auto', margin: 0, background: 'var(--surface-inset)', border: '1px solid var(--hairline)', borderRadius: 8, padding: 12 }}>
                {transcript}
              </pre>
            </>
          )}
        </Modal>

        {/* ---- 2026-09-04：历史记录详情浮窗（转写稿/总结/生成/重新分析/操作全在浮窗内） ---- */}
        <DraggableModal title={`记录详情${view?.created_at ? ` · ${view.created_at?.slice(0, 16).replace('T', ' ')}` : ''}`}
          open={!!view} onCancel={() => setView(null)} footer={null} width={720}>
          {view && (
            <div style={{ fontSize: 12, lineHeight: 1.7 }}>
              <Space size={8} style={{ marginBottom: 12, flexWrap: 'wrap' }}>
                <Tag color={STATUS_MAP[view.status]?.color || 'default'}>{STATUS_MAP[view.status]?.text || view.status}</Tag>
                {view.duration_s ? <span style={{ color: 'var(--text-3)' }}>{Math.round(view.duration_s / 60)} 分钟</span> : null}
                {view.scene && <span style={{ color: 'var(--text-3)' }}>场景：{view.scene}</span>}
                {view.error_msg && <span style={{ color: 'var(--error)' }}>{view.error_msg.slice(0, 100)}</span>}
                {['transcribing', 'summarizing'].includes(view.status) &&
                  <span style={{ color: 'var(--text-3)' }}>处理中，完成后浮窗自动更新…</span>}
              </Space>
              {view.summary ? (
                <>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}><Icon as={ClipboardText} /> 总结</div>
                  <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, maxHeight: 260, overflow: 'auto', margin: 0, background: 'var(--surface-inset)', border: '1px solid var(--hairline)', borderRadius: 8, padding: 12 }}>{view.summary}</pre>
                </>
              ) : (view.status === 'ready' && !vEditOpen && (
                <div style={{ color: 'var(--text-3)', marginBottom: 8 }}>转写完成，未生成总结——在下方选择场景和模型生成</div>
              ))}
              {/* 生成/重新分析表单（ready 未总结默认展开；done 点「重新分析」展开） */}
              {(vEditOpen || (view.status === 'ready' && !view.summary)) && (
                <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12, background: 'var(--surface-inset)' }}>
                  <div style={{ fontWeight: 600, marginBottom: 8 }}>
                    {view.summary
                      ? <><Icon as={ArrowsClockwise} /> 重新分析（换场景/模型重新总结）</>
                      : <><Icon as={Sparkle} /> 开始分析（选场景/模型生成总结）</>}
                  </div>
                  <Space direction="vertical" style={{ width: '100%' }} size={8}>
                    {/* 2026-09-04：标签固定列宽分行走排，避免场景/模型/按钮挤一行 */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span style={{ color: 'var(--text-2)', width: 64 }}>总结场景</span>
                      <Select placeholder="选择总结场景" style={{ width: 240 }}
                        options={[
                          ...scenes.map((s) => ({ value: s.key, label: s.label })),
                          { value: 'custom', label: '自定义提示词' },
                        ]}
                        value={vScene} onChange={setVScene} />
                      <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                        {vScene === 'custom' ? '（提示词必填）' : '（场景可选）'}
                      </span>
                    </div>
                    <TextArea rows={3}
                      placeholder={vScene === 'custom'
                        ? '请输入自定义总结要求（必填，如：按 hr 面试评估维度总结）'
                        : '额外分析要求（可选，如：按 hr 面试评估维度总结）——在场景预设基础上补充关注点'}
                      value={vPrompt} onChange={(e) => setVPrompt(e.target.value)} />
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span style={{ color: 'var(--text-2)', width: 64 }}>总结模型</span>
                      <Select size="small" style={{ width: 240 }} placeholder="默认（团队配置档）" allowClear
                        value={vModel ? `${vModel.platform}|${vModel.model}` : undefined}
                        onChange={(v: string | undefined) => {
                          if (!v) { setVModel(null); return }
                          const [platform, model] = String(v).split('|')
                          setVModel({ platform, model, thinking: null })
                        }}
                        options={(modelOptions.llm_aux || []).map((m: any) => ({
                          value: `${m.platform}|${m.model}`, label: `${m.model}（${m.platform}）`,
                        }))} />
                      {vModel?.platform === 'deepseek' && (
                        <Select size="small" style={{ width: 140 }} placeholder="思考强度"
                          value={vModel.thinking == null ? 'default' : vModel.thinking}
                          onChange={(v) => setVModel({ ...vModel, thinking: v === 'default' ? null : String(v) })}
                          options={[
                            { value: 'default', label: '默认（跟随配置）' },
                            ...(modelOptions.thinking || []).filter((t: any) => t.key !== null).map((t: any) => ({
                              value: String(t.key), label: t.label,
                            })),
                          ]} />
                      )}
                    </div>
                    <div style={{ textAlign: 'right' }}>
                      <Button type="primary" size="small" loading={vGenerating}
                        disabled={vGenerating || view.status === 'summarizing'}
                        onClick={() => runSummary(view.id)}>
                        {view.summary ? '重新生成总结' : '开始分析'}
                      </Button>
                    </div>
                  </Space>
                </div>
              )}
              {view.transcript && (
                <>
                  <div style={{ fontWeight: 600, margin: '12px 0 6px' }}><Icon as={NotePencil} /> 转写稿（{view.transcript.length} 字）</div>
                  <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, maxHeight: 240, overflow: 'auto', margin: 0, background: 'var(--surface-inset)', border: '1px solid var(--hairline)', borderRadius: 8, padding: 12 }}>{view.transcript}</pre>
                </>
              )}
              <Space style={{ marginTop: 14 }}>
                {/* 2026-09-11（走查：总结中点下载 → 400「总结尚未生成」，前端只显示"下载失败"）：
                    完整档案含总结，仅 done 可下载；总结中/待总结给出禁用态与原因；失败显示后端 message */}
                {view.status === 'done' ? (
                  <Button size="small" type="primary" icon={<Icon as={DownloadSimple} />}
                    onClick={() => downloadByUrl(toolsApi.meetingArchiveUrl(view.id))}>下载完整内容</Button>
                ) : ['ready', 'summarizing'].includes(view.status) && (
                  <Tooltip title="完整档案含总结，总结完成后才能下载">
                    <Button size="small" icon={<Icon as={DownloadSimple} />} disabled>下载完整内容</Button>
                  </Tooltip>
                )}
                {view.status === 'failed' && (
                  <Button size="small" icon={<Icon as={DownloadSimple} />} onClick={() => retryTranscribe(view.id)}>重新转写</Button>
                )}
                {view.status === 'done' && !vEditOpen && (
                  <Button size="small" onClick={() => { setVEditOpen(true); setVScene(view.scene || vScene); setVPrompt('') }}>重新分析</Button>
                )}
                {view.status === 'done' && vEditOpen && (
                  <Button size="small" onClick={() => setVEditOpen(false)}>收起</Button>
                )}
                {!['transcribing', 'summarizing'].includes(view.status) && (
                  <Popconfirm title="删除该录音记录？" onConfirm={() => { deleteOne(view.id); setView(null) }}>
                    <Button size="small" danger icon={<Icon as={Trash} />}>删除</Button>
                  </Popconfirm>
                )}
              </Space>
            </div>
          )}
        </DraggableModal>
      </div>
    </div>
  )
}
