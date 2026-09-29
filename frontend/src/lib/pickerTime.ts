// antd TimePicker「手输不回车」的值提交坑（2026-09-17，用户报"定时改了没生效"）
//
// 现象：在时间框里手输 `12:05` 而不按回车，rc-picker 不会提交这个值——表单里仍是**旧值**，
// 而保存照样成功 → 界面上显示的是新时间、存进去的是旧的，人毫无察觉（实测复现）。
// 解法：提交前以**输入框里实际显示的文本**为准。Form.Item 的 name 会渲染成 input 的 id，
// 按 name 取 DOM 即可（从面板点选时，文本与表单值本来就一致，取哪个都一样）。
//
// 用法（定时类表单统一）：
//   const t = readPickerTime('schedule_time')
//   if (t.bad) { message.warning(BAD_PICKER_TIME_MSG); return }
//   payload.schedule_time = t.text || formValue?.format('HH:mm') || ''
export function readPickerTime(fieldName: string): { text: string; bad: boolean } {
  const el = document.getElementById(fieldName) as HTMLInputElement | null
  const t = (el?.value || '').trim()
  if (!t) return { text: '', bad: false }
  const m = /^(\d{1,2}):(\d{2})$/.exec(t)          // 接受 9:30 / 09:30
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return { text: t, bad: true }
  return { text: `${String(m[1]).padStart(2, '0')}:${m[2]}`, bad: false }
}

export const BAD_PICKER_TIME_MSG = '时间格式不对——请用 HH:MM（如 09:30），或点开时间面板直接选'
