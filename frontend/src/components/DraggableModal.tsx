// 可拖拽 Modal：按住标题栏拖动移动（AntD Modal 包装，其余 props 透传）
// 用于业务操作浮窗（admin 反馈详情/回复处理、各页面编辑弹窗等），标题栏显示抓手光标
import { useEffect, useRef } from 'react'
import { Modal } from 'antd'
import type { ModalProps } from 'antd'

export default function DraggableModal(props: ModalProps) {
  // L13：document 级监听器卸载兜底（mouseup 丢失——拖出窗口/拖动中卸载——时残留）
  const cleanupRef = useRef<(() => void) | null>(null)

  const onTitleMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.button !== 0) return  // 仅左键拖动
    const modalEl = (e.currentTarget as HTMLElement).closest('.ant-modal') as HTMLElement | null
    if (!modalEl) return
    const startX = e.clientX
    const startY = e.clientY
    const rect = modalEl.getBoundingClientRect()
    const move = (ev: MouseEvent) => {
      modalEl.style.position = 'fixed'
      modalEl.style.left = `${rect.left + ev.clientX - startX}px`
      modalEl.style.top = `${rect.top + ev.clientY - startY}px`
      modalEl.style.margin = '0'
    }
    const up = () => {
      document.removeEventListener('mousemove', move)
      document.removeEventListener('mouseup', up)
      cleanupRef.current = null
    }
    document.addEventListener('mousemove', move)
    document.addEventListener('mouseup', up)
    cleanupRef.current = () => {
      document.removeEventListener('mousemove', move)
      document.removeEventListener('mouseup', up)
    }
    e.preventDefault()
  }

  // 卸载时清理残留监听器
  useEffect(() => {
    return () => { cleanupRef.current?.() }
  }, [])

  return (
    <Modal
      {...props}
      // 默认垂直居中（AntD 默认偏上）；拖拽后按拖拽位置固定
      centered={props.centered ?? true}
      title={
        <div style={{ cursor: 'move', userSelect: 'none', paddingRight: 24 }} onMouseDown={onTitleMouseDown}>
          {props.title}
        </div>
      }
    />
  )
}
