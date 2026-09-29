// 大文件下载直链（2026-09-15）：<a href> 同源 GET + httpOnly cookie 鉴权 → 浏览器原生下载。
//
// 为什么不用 fetch→blob：blob 会把**整包读进浏览器内存**（大 zip/视频顶内存），且无原生进度、
// 失败要从头再来。直链则流式落盘 + 原生进度条 + 浏览器自带失败重试，零 JS 内存。
//
// 约定（2026-09-15 用户要求）：**本函数不做任何 URL 拼接/前缀嗅探**——传入的必须是把 API 前缀
// 拼好的完整 URL（来源两种：① 接口返回的 URL（如产出物 download_url）；② api/*.ts 里的
// `*Url()` 构造器，前缀只在 api 层拼一次）。这样以后换动态路由/前缀时，改动只落在 api 层。
// 文件名由服务端 Content-Disposition 提供（download 置空串即用服务端名；传 fallbackName 可覆盖）。
//
// 例外：需要自定义请求头（如运维视角的 X-Dept-Id）的下载无法直链——<a href> 带不了头，
// 需要带鉴权头的接口保留 fetch→blob。
export function downloadByUrl(url: string, fallbackName?: string): void {
  const a = document.createElement('a')
  a.href = url
  a.download = fallbackName || ''   // 空串 = 用服务端 Content-Disposition 的文件名
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}
