// 上传进度条（2026-09-10）：三段进度（校验/分片/服务端合并）+ 取消 + 中断重试（续传）
// 大文件上传共用（压缩包 / 音视频 / 会议纪要），进度与续传模型见 lib/chunkUpload.ts
import type { CSSProperties } from 'react'
import { Button, Progress } from 'antd'
import type { UploadProgress as Prog } from '../lib/chunkUpload'

export default function UploadProgress({ progress, onCancel, error, onRetry, style }: {
  progress: Prog
  onCancel?: () => void
  /** 中断原因（有值=异常态：显示重试/放弃；分片已传部分保留在服务端，重试只补缺口） */
  error?: string
  onRetry?: () => void
  style?: CSSProperties
}) {
  const done = progress.phase === 'done'
  const failed = !!error
  return (
    <div style={{ marginTop: 10, ...style }}>
      <Progress percent={Math.round(progress.percent)} size="small"
        status={failed ? 'exception' : done ? 'success' : 'active'} />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 12, color: failed ? '#cf1322' : 'var(--text-2)' }}>
          {failed ? error : progress.text}
        </span>
        <span style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
          {failed && onRetry && (
            <Button size="small" type="primary" style={{ fontSize: 12 }} onClick={onRetry}>重试续传</Button>
          )}
          {failed && onCancel && (
            <Button size="small" type="link" style={{ padding: 0, fontSize: 12 }} onClick={onCancel}>放弃</Button>
          )}
          {!failed && onCancel && !done && (
            <Button size="small" type="link" danger style={{ padding: 0, fontSize: 12 }}
              onClick={onCancel}>取消上传</Button>
          )}
        </span>
      </div>
    </div>
  )
}
